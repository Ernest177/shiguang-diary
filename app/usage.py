"""Local request ledger. No prompts, responses, diary text or credentials are stored."""
from __future__ import annotations

import contextlib
from contextvars import ContextVar
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import threading
from urllib.parse import urlsplit
import uuid

from app.store import tzinfo

CALL_SCOPE = ContextVar("diary_call_scope", default={})
PRICE_SOURCE = "https://help.aliyun.com/zh/model-studio/model-pricing"
KINDS = {"entry": "单条整理", "day": "每日回顾", "memory": "长期记忆", "growth": "成长片段", "portrait": "个人画像", "month": "月度回顾", "test": "连接测试", "other": "其他请求"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS prices(
 provider TEXT NOT NULL,model TEXT NOT NULL,input_rate TEXT NOT NULL,output_rate TEXT NOT NULL,
 cached_rate TEXT,source TEXT NOT NULL,checked_at TEXT NOT NULL,PRIMARY KEY(provider,model));
CREATE TABLE IF NOT EXISTS calls(
 id TEXT PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT,kind TEXT NOT NULL,target TEXT NOT NULL,
 job_id TEXT NOT NULL,attempt INTEGER NOT NULL,model TEXT NOT NULL,returned_model TEXT,provider TEXT NOT NULL,
 status TEXT NOT NULL,input_tokens INTEGER,output_tokens INTEGER,total_tokens INTEGER,cached_tokens INTEGER,
 reasoning_tokens INTEGER,total_source TEXT,usage_note TEXT,latency_ms INTEGER,input_rate TEXT,output_rate TEXT,
 cached_rate TEXT,estimated_cost TEXT,cost_basis TEXT,price_source TEXT,price_checked_at TEXT,error_code TEXT);
CREATE INDEX IF NOT EXISTS calls_time ON calls(started_at DESC,id DESC);
PRAGMA user_version=1;
"""


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@contextlib.contextmanager
def call_scope(kind, target="", job_id="", attempt=1):
    token = CALL_SCOPE.set({"kind": kind if kind in KINDS else "other", "target": target, "job_id": job_id, "attempt": attempt})
    try:
        yield
    finally:
        CALL_SCOPE.reset(token)


def clean(value, limit=200):
    return "".join(c for c in str(value or "") if ord(c) >= 32)[:limit]


def provider_key(settings):
    base = settings.get("ai_base_url", "").rstrip("/")
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    return hashlib.sha256(endpoint.encode()).hexdigest()


def number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**12 else None


def usage_counts(raw):
    raw = raw if isinstance(raw, dict) else {}
    counts = {key: number(raw.get(key)) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    prompt, output = counts["prompt_tokens"], counts["completion_tokens"]
    total_source, notes = "provider", []
    if counts["total_tokens"] is None and prompt is not None and output is not None:
        counts["total_tokens"] = prompt + output
        total_source = "sum"
    elif prompt is not None and output is not None and counts["total_tokens"] != prompt + output:
        counts["total_tokens"] = None
        notes.append("总量与输入输出之和不一致")
    if counts["total_tokens"] is None:
        total_source = "unknown"
    prompt_details = raw.get("prompt_tokens_details")
    output_details = raw.get("completion_tokens_details")
    cached = number(prompt_details.get("cached_tokens")) if isinstance(prompt_details, dict) else None
    reasoning = number(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else None
    if cached is not None and (prompt is None or cached > prompt):
        cached = None
        notes.append("缓存用量无效")
    if reasoning is not None and (output is None or reasoning > output):
        reasoning = None
        notes.append("思考用量无效")
    return {"input_tokens": prompt, "output_tokens": output, "total_tokens": counts["total_tokens"],
            "cached_tokens": cached, "reasoning_tokens": reasoning, "total_source": total_source,
            "usage_note": "；".join(notes) or None}


def rate(value, optional=False):
    if optional and value in (None, ""):
        return None
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError("请填写有效单价，单位为元 / 百万 Token")
    try:
        result = Decimal(str(value))
        if not result.is_finite() or not 0 <= result <= 1000000 or result.as_tuple().exponent < -8:
            raise ValueError("单价需为非负数，最多 8 位小数")
        return format(result, "f")
    except (InvalidOperation, TypeError):
        raise ValueError("单价格式无效") from None


def estimate(counts, price):
    if counts["input_tokens"] is None or counts["output_tokens"] is None or not price:
        return None, "unknown"
    prompt, output = Decimal(counts["input_tokens"]), Decimal(counts["output_tokens"])
    cached = counts["cached_tokens"]
    if cached is not None and price.get("cached_rate") is not None:
        cost = (prompt - cached) * Decimal(price["input_rate"]) + Decimal(cached) * Decimal(price["cached_rate"])
        basis = "cached_rate"
    else:
        cost = prompt * Decimal(price["input_rate"])
        basis = "standard_rates"
    cost += output * Decimal(price["output_rate"])
    return format((cost / 1000000).quantize(Decimal("0.00000001")), "f"), basis


class UsageLedger:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "usage.sqlite3"
        self.lock = threading.RLock()
        self.warning = ""
        with self.connection() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise ValueError("用量记录来自更新版本，请升级程序后打开")
            db.executescript(SCHEMA)
            db.execute("UPDATE calls SET status='interrupted',finished_at=?,error_code='service_restarted' WHERE status='running'", (stamp(),))

    @contextlib.contextmanager
    def connection(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            try:
                with db:
                    yield db
            finally:
                db.close()

    def price(self, settings):
        key, model = provider_key(settings), settings.get("ai_model", "")
        with self.connection() as db:
            row = db.execute("SELECT * FROM prices WHERE provider=? AND model=?", (key, model)).fetchone()
        if row:
            return dict(row)
        host = urlsplit(settings.get("ai_base_url", "")).hostname or ""
        # Only this verified model/region has a preset; other services require their own rates.
        if model == "qwen3.8-flash" and (host == "dashscope.aliyuncs.com" or host.endswith(".cn-beijing.maas.aliyuncs.com")):
            return {"provider": key, "model": model, "input_rate": "0.8", "output_rate": "2.7", "cached_rate": None,
                    "source": PRICE_SOURCE, "checked_at": "2026-10-20"}
        return None

    def save_price(self, settings, payload):
        if payload.get("model") != settings.get("ai_model") or payload.get("provider") != provider_key(settings):
            raise ValueError("模型或服务已变更，请刷新后重新设置单价")
        values = (provider_key(settings), settings["ai_model"], rate(payload.get("input_rate")),
                  rate(payload.get("output_rate")), rate(payload.get("cached_rate"), True), "user", stamp())
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?,?,?)", values)

    def start(self, settings):
        scope = CALL_SCOPE.get()
        price = self.price(settings) or {}
        row = {"id": str(uuid.uuid4()), "started_at": stamp(), "kind": scope.get("kind", "other"),
               "target": clean(scope.get("target")), "job_id": clean(scope.get("job_id")),
               "attempt": scope.get("attempt", 1), "model": clean(settings.get("ai_model")),
               "provider": provider_key(settings), "status": "running", "input_rate": price.get("input_rate"),
               "output_rate": price.get("output_rate"), "cached_rate": price.get("cached_rate"),
               "price_source": price.get("source"), "price_checked_at": price.get("checked_at")}
        with self.connection() as db:
            db.execute("INSERT INTO calls(" + ",".join(row) + ") VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))
        return row["id"]

    def finish(self, identifier, usage, returned_model, status, latency_ms, error_code=None):
        counts = usage_counts(usage)
        with self.connection() as db:
            row = dict(db.execute("SELECT * FROM calls WHERE id=?", (identifier,)).fetchone())
            price = row if row["input_rate"] is not None and row["output_rate"] is not None else None
            cost, basis = estimate(counts, price)
            updates = {**counts, "returned_model": clean(returned_model) or None, "status": status,
                       "finished_at": stamp(), "latency_ms": latency_ms, "error_code": error_code,
                       "estimated_cost": cost, "cost_basis": basis}
            db.execute("UPDATE calls SET " + ",".join(k + "=?" for k in updates) + " WHERE id=?", (*updates.values(), identifier))

    def bounds(self, month, zone):
        if month == "all":
            return "", []
        try:
            start = datetime.strptime(month, "%Y-%m").replace(tzinfo=tzinfo(zone))
            if start.strftime("%Y-%m") != month:
                raise ValueError()
            end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        except (ValueError, TypeError, OverflowError):
            raise ValueError("请选择有效月份") from None
        return " WHERE started_at>=? AND started_at<?", [start.astimezone(timezone.utc).isoformat(timespec="milliseconds"), end.astimezone(timezone.utc).isoformat(timespec="milliseconds")]

    def growth_usage(self, settings):
        local = datetime.now(tzinfo(settings["timezone"]))
        where, args = self.bounds(local.strftime("%Y-%m"), settings["timezone"])
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat(timespec="milliseconds")
        with self.connection() as db:
            rows = db.execute("SELECT started_at,estimated_cost,status FROM calls" + where +
                              " AND kind IN ('growth','portrait','month')", args).fetchall()
        cost = sum((Decimal(r["estimated_cost"]) for r in rows if r["estimated_cost"] is not None), Decimal(0))
        return {"today_calls": sum(r["started_at"] >= start for r in rows), "month_cost": format(cost, ".8f"),
                "unknown_cost": sum(r["estimated_cost"] is None for r in rows), "priced": bool(self.price(settings))}

    def state(self, settings, month, offset=0, limit=50):
        if not isinstance(offset, int) or not 0 <= offset <= 1000000:
            raise ValueError("分页位置无效")
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("每页数量无效")
        where, args = self.bounds(month, settings["timezone"])
        with self.connection() as db:
            totals = dict(db.execute("SELECT count(*) AS requests,coalesce(sum(input_tokens),0) AS input_tokens,"
                "coalesce(sum(output_tokens),0) AS output_tokens,coalesce(sum(total_tokens),0) AS total_tokens,"
                "coalesce(sum(input_tokens IS NULL),0) AS missing_input,coalesce(sum(output_tokens IS NULL),0) AS missing_output,"
                "coalesce(sum(total_tokens IS NULL),0) AS missing_total,coalesce(sum(estimated_cost IS NULL),0) AS unknown_cost FROM calls" + where, args).fetchone())
            cost = sum((Decimal(r[0]) for r in db.execute("SELECT estimated_cost FROM calls" + where + (" AND" if where else " WHERE") + " estimated_cost IS NOT NULL", args)), Decimal(0))
            totals["known_cost"] = format(cost, "f")
            records = [dict(r) for r in db.execute("SELECT * FROM calls" + where + " ORDER BY started_at DESC,id DESC LIMIT ? OFFSET ?", (*args, limit, offset))]
        return {"month": month, "offset": offset, "limit": limit, "summary": totals, "records": records,
                "price": self.price(settings), "provider": provider_key(settings), "model": settings.get("ai_model", ""),
                "warning": self.warning}

    def export_csv(self, month, zone):
        where, args = self.bounds(month, zone)
        with self.connection() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM calls" + where + " ORDER BY started_at,id", args)]
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        columns = ("id", "started_at", "kind", "job_id", "attempt", "model", "returned_model", "status", "input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens", "estimated_cost", "input_rate", "output_rate", "cached_rate", "cost_basis", "error_code", "price_source", "price_checked_at")
        writer.writerow(columns)
        for row in rows:
            values = ["" if row[c] is None else str(row[c]) for c in columns]
            writer.writerow(["'" + v if v.lstrip().startswith(("=", "+", "-", "@")) else v for v in values])
        return ("\ufeff" + stream.getvalue()).encode("utf-8")

    def backup(self, settings):
        """Independent daily snapshots; restoring a diary never rewinds spending."""
        folder = Path(settings.get("backup_dir") or self.directory / "backups").expanduser().resolve()
        folder.mkdir(parents=True, exist_ok=True)
        name = "usage-" + datetime.now(tzinfo(settings.get("timezone", "Asia/Shanghai"))).strftime("%Y%m%d") + ".sqlite3"
        temporary = folder / (name + ".partial")
        with self.lock:
            try:
                with contextlib.closing(sqlite3.connect(self.path)) as source, contextlib.closing(sqlite3.connect(temporary)) as target:
                    source.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("用量快照完整性检查失败")
                os.replace(temporary, folder / name)
                for old in sorted(folder.glob("usage-????????.sqlite3"), reverse=True)[settings.get("backup_keep", 30):]:
                    old.unlink()
            finally:
                temporary.unlink(missing_ok=True)
