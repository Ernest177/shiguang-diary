"""Incremental, source-versioned growth archive. Originals remain the source of truth."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from app.about import encode, signature, fingerprint, source_hash, stamp

GROWTH_TABLES = ("memory_exclusions", "growth_receipts")
GROWTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_exclusions(
 entry_id TEXT PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS growth_receipts(
 record_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,content_hash TEXT NOT NULL,
 prompt_version TEXT NOT NULL,next_chunk INTEGER NOT NULL,total_chunks INTEGER NOT NULL,
 data TEXT NOT NULL,model TEXT NOT NULL,updated_at TEXT NOT NULL);
"""
GROWTH_DEFAULTS = {"growth_enabled": False, "growth_paused": False,
                   "growth_daily_calls": 12, "growth_monthly_budget": "1.00"}
CHUNK_SIZE, CHUNK_STEP = 4000, 3800


def chunks(record):
    body = record["body"]
    return [body[i:i + CHUNK_SIZE] for i in range(0, max(1, len(body) - 200), CHUNK_STEP)]


def record_hash(record):
    return signature(record)


def growth_fingerprint(category, title, record_id):
    return signature(["growth.v1", fingerprint(category, title), record_id])


def validate_growth(data, record):
    from app.ai import AIError, array, evidence, text
    from app.about_ai import validate_memories
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise AIError("模型没有返回有效的成长线索；日记整理已保留，可单独重试成长记录")
    if not isinstance(data.get("memories"), list) or len(data["memories"]) > 3:
        raise AIError("每个片段最多保留三条成长线索")
    result = validate_memories(data, {"records": [record]})
    events = []
    for item in array(data, "events", 2):
        if item.get("kind") not in ("milestone", "turning_point", "moment"):
            raise AIError("成长片段类别无效")
        quote = evidence(item.get("quote"), record["body"])
        if len(quote) > 600:
            raise AIError("成长片段引用过长")
        events.append({"title": text(item.get("title"), 100), "detail": text(item.get("detail"), 400),
                       "kind": item["kind"], "quote": quote})
    return {**result, "events": events}


class GrowthMixin:
    def growth_record(self, record_id, db):
        kind, identifier = record_id.split(":", 1)
        if kind == "entry":
            row = db.execute("SELECT * FROM entries WHERE id=? AND ai_enabled=1", (identifier,)).fetchone()
            permitted = (not db.execute("SELECT 1 FROM memory_exclusions WHERE entry_id=?", (identifier,)).fetchone()) if self.settings(db)["growth_enabled"] else db.execute("SELECT 1 FROM memory_permissions WHERE entry_id=?", (identifier,)).fetchone()
            if row and permitted:
                return {"id": record_id, "type": kind, "body": row["body"], "revision": row["revision"],
                        "period": row["journal_day"], "title": row["journal_day"] + "的日记"}
        elif kind == "experience":
            row = db.execute("SELECT * FROM experiences WHERE id=? AND ai_enabled=1", (identifier,)).fetchone()
            if row:
                return {"id": record_id, "type": kind, "body": row["body"], "revision": row["revision"], "period": row["period"], "title": row["title"]}
        return None

    def receipt_current(self, record, receipt):
        from app.prompts import GROWTH_PROMPT_VERSION
        return bool(receipt and receipt["revision"] == record["revision"] and
                    receipt["content_hash"] == record_hash(record) and receipt["prompt_version"] == GROWTH_PROMPT_VERSION)

    def growth_plan(self, db):
        receipts = {r["record_id"]: dict(r) for r in db.execute("SELECT * FROM growth_receipts")}
        pending, complete, parts = [], 0, 0
        for record in self.about_records(db):
            receipt = receipts.get(record["id"])
            current = self.receipt_current(record, receipt)
            if current and receipt["next_chunk"] == receipt["total_chunks"]:
                complete += 1
            else:
                pending.append(record)
                parts += len(chunks(record)) - (receipt["next_chunk"] if current else 0)
        return pending, complete, parts

    def schedule_growth(self, db, retry_failed=False):
        settings = self.settings(db)
        if not settings["growth_enabled"] or settings["growth_paused"]:
            return
        pending, _, _ = self.growth_plan(db)
        for record in pending:
            job = db.execute("SELECT status FROM jobs WHERE kind='growth' AND target=?", (record["id"],)).fetchone()
            # A new entry will supply these fields in its ordinary analysis request.
            ordinary = record["type"] == "entry" and db.execute(
                "SELECT 1 FROM jobs WHERE kind='entry' AND target=? AND status IN ('queued','running','waiting')",
                (record["id"].split(":", 1)[1],)).fetchone()
            if ordinary or (job and not (retry_failed and job[0] == "failed")):
                continue
            self.enqueue(db, "growth", record["id"], record["revision"])

    def set_growth_policy(self, values):
        from decimal import Decimal, InvalidOperation
        with self.connection() as db:
            policy = {k: values.get(k, self.settings(db)[k]) for k in GROWTH_DEFAULTS}
            if not all(isinstance(policy[k], bool) for k in ("growth_enabled", "growth_paused")):
                raise ValueError("请选择有效的自动积累设置")
            calls = policy["growth_daily_calls"]
            if isinstance(calls, bool) or not isinstance(calls, int) or not 1 <= calls <= 100:
                raise ValueError("每日额外请求上限需为 1–100 次")
            try:
                budget = Decimal(str(policy["growth_monthly_budget"]))
                if not budget.is_finite() or not 0 < budget <= 10000 or budget.as_tuple().exponent < -2:
                    raise ValueError()
                policy["growth_monthly_budget"] = format(budget, ".2f")
            except (ValueError, InvalidOperation):
                raise ValueError("每月参考预算需大于 0，最多两位小数") from None
            if policy["growth_enabled"] and self.settings(db)["ai_mode"] == "off":
                raise ValueError("请先配置 AI 服务，再启用自动积累")
            was_enabled = self.settings(db)["growth_enabled"]
            if was_enabled and not policy["growth_enabled"]:
                for record in self.about_records(db):
                    if record["type"] == "entry":
                        db.execute("INSERT OR IGNORE INTO memory_permissions VALUES(?)", (record["id"].split(":", 1)[1],))
            for key, value in policy.items():
                db.execute("UPDATE settings SET value=? WHERE key=?", (encode(value), key))
            # Cancel tickets as well as queued jobs, so late responses cannot publish.
            for job in db.execute("SELECT id FROM jobs WHERE kind IN ('growth','portrait','memory','month') AND status='running'").fetchall():
                db.execute("UPDATE jobs SET id=?,status='failed',error='处理期间设置发生改变，结果已撤回；如需继续请手动重试' WHERE id=?", (str(uuid.uuid4()), job[0]))
            db.execute("DELETE FROM jobs WHERE kind IN ('growth','portrait','memory','month') AND status NOT IN ('failed','running')")
            if was_enabled != policy["growth_enabled"]:
                db.execute("DELETE FROM personal_reports")
            self.schedule_growth(db)
            if policy["growth_enabled"] and not policy["growth_paused"]:
                self.schedule_portrait(db)
            return policy

    def validate_growth_import(self, tables):
        from app.ai import AIError
        records = {"entry:" + r["id"]: {"id": "entry:" + r["id"], "type": "entry", "body": r["body"],
                   "revision": r["revision"], "period": r["journal_day"], "title": r["journal_day"] + "的日记"} for r in tables["entries"]}
        records.update({"experience:" + r["id"]: {"id": "experience:" + r["id"], "type": "experience", "body": r["body"],
                        "revision": r["revision"], "period": r["period"], "title": r["title"]} for r in tables["experiences"]})
        permitted = {"entry:" + r["entry_id"] for r in tables["memory_permissions"]}
        permitted |= {"experience:" + r["id"] for r in tables["experiences"] if r["ai_enabled"]}
        excluded = {"entry:" + r["entry_id"] for r in tables["memory_exclusions"]}
        if permitted & excluded:
            raise ValueError("备份成长来源同时包含与排除")
        for receipt in tables["growth_receipts"]:
            record = records.get(receipt["record_id"])
            if not record or record["id"] not in permitted or receipt["revision"] != record["revision"] or receipt["content_hash"] != record_hash(record):
                raise ValueError("备份成长缓存与来源不一致")
            parts = chunks(record)
            # Piggyback analysis may cover the entire entry in one request.
            if receipt["total_chunks"] == 1:
                parts = [record["body"]]
            if receipt["total_chunks"] != len(parts) or not 0 <= receipt["next_chunk"] <= len(parts):
                raise ValueError("备份成长处理进度无效")
            data = json.loads(receipt["data"])
            if not isinstance(data, dict) or not isinstance(data.get("chunks"), list) or len(data["chunks"]) != receipt["next_chunk"]:
                raise ValueError("备份成长缓存格式无效")
            if not isinstance(receipt["prompt_version"], str) or len(receipt["prompt_version"]) > 80:
                raise ValueError("备份成长缓存版本无效")
            try:
                for i, value in enumerate(data["chunks"]):
                    validate_growth(value, {**record, "body": parts[i]})
                datetime.fromisoformat(receipt["updated_at"])
            except AIError as exc:
                raise ValueError("备份成长缓存缺少原文依据") from exc

    def schedule_portrait(self, db, immediate=False):
        if not self.settings(db)["growth_enabled"] or self.settings(db)["growth_paused"]:
            return
        context, current = self.portrait_context(db)
        if not context["memories"]:
            return
        existing = db.execute("SELECT status FROM jobs WHERE kind='portrait' AND target='current'").fetchone()
        if existing:
            if existing[0] == "running":
                return
            if existing[0] == "queued":
                if immediate:
                    db.execute("UPDATE jobs SET retry_at=NULL WHERE kind='portrait' AND target='current'")
                return
            if existing[0] in ("failed", "deferred") and not immediate:
                return
        cached = db.execute("SELECT signature FROM personal_reports WHERE kind='portrait' AND target='current'").fetchone()
        if cached and cached[0] == current:
            return
        self.enqueue(db, "portrait", "current")
        if not immediate:
            later = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat(timespec="milliseconds")
            db.execute("UPDATE jobs SET retry_at=? WHERE kind='portrait'", (later,))

    def growth_status(self, db=None):
        if db is None:
            with self.connection() as db:
                return self.growth_status(db)
        pending, complete, parts = self.growth_plan(db)
        settings = self.settings(db)
        states = {r[0]: r[1] for r in db.execute("SELECT status,count(*) FROM jobs WHERE kind='growth' GROUP BY status")}
        return {"enabled": settings["growth_enabled"], "paused": settings["growth_paused"],
                "daily_calls": settings["growth_daily_calls"], "monthly_budget": settings["growth_monthly_budget"],
                "complete": complete, "pending": len(pending), "total": complete + len(pending),
                "remaining_chunks": parts, "failed": states.get("failed", 0),
                "running": states.get("running", 0), "deferred": states.get("deferred", 0),
                "notice": next((r[0] for r in db.execute("SELECT error FROM jobs WHERE kind IN ('growth','portrait') AND status='deferred' ORDER BY updated_at DESC LIMIT 1")), "")}

    def save_growth_chunk(self, db, record, result, model, index, total):
        from app.prompts import GROWTH_PROMPT_VERSION
        receipt = db.execute("SELECT * FROM growth_receipts WHERE record_id=?", (record["id"],)).fetchone()
        saved = json.loads(receipt["data"])["chunks"] if self.receipt_current(record, receipt) else []
        if len(saved) != index:
            return False
        saved.append(result)
        db.execute("INSERT OR REPLACE INTO growth_receipts VALUES(?,?,?,?,?,?,?,?,?)",
                   (record["id"], record["revision"], record_hash(record), GROWTH_PROMPT_VERSION,
                    index + 1, total, encode({"chunks": saved}), model, stamp()))
        protected_hashes = {source_hash(m["category"], s) for m in self.memories(db) if m["user_modified"] for s in m["sources"]}
        blocked = [dict(r) for r in db.execute("SELECT * FROM memory_suppressions")]
        blocked_keys = {r["fingerprint"] for r in blocked}
        blocked_hashes = {h for r in blocked for h in json.loads(r["source_hashes"])}
        for memory in result["memories"]:
            key = growth_fingerprint(memory["category"], memory["title"], record["id"])
            hashes = {source_hash(memory["category"], s) for s in memory["sources"]}
            if key in blocked_keys or fingerprint(memory["category"], memory["title"]) in blocked_keys or hashes & (blocked_hashes | protected_hashes):
                continue
            old = db.execute("SELECT * FROM memories WHERE fingerprint=?", (key,)).fetchone()
            if old:
                continue
            identifier = old["id"] if old else str(uuid.uuid4())
            if not old:
                updated = stamp()
                db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (identifier, key, memory["title"], memory["detail"],
                           memory["category"], memory["horizon"], memory["period"], "pending", "", 0, updated, updated))
            kind, source_id = record["id"].split(":", 1)
            quote = memory["sources"][0]["quote"]
            db.execute("INSERT OR IGNORE INTO memory_sources VALUES(?,?,?,?,?)", (identifier, source_id if kind == "entry" else None,
                       source_id if kind == "experience" else None, record["revision"], quote))
        # Reports validate their signatures before display/use. Keep identical
        # portrait results when a source produced no new memory.
        self.schedule_portrait(db)
        return True

    def timeline(self, query="", kind="all", offset=0):
        if kind not in ("all", "milestone", "turning_point", "moment") or not isinstance(offset, int) or not 0 <= offset <= 1000000:
            raise ValueError("成长时间线查询无效")
        query = str(query).strip()[:100]
        with self.connection() as db:
            # Search originals locally, including private entries. Never calls a model.
            sql = """SELECT 'entry:'||id AS record_id,journal_day AS period,journal_day||'的日记' AS title,
                     body,created_at FROM entries WHERE instr(lower(body),lower(?))>0
                     UNION ALL SELECT 'experience:'||id,period,title,body,created_at FROM experiences
                     WHERE instr(lower(body||title),lower(?))>0"""
            rows = [dict(r) for r in db.execute(sql, (query, query))]
            events = {}
            for receipt in db.execute("SELECT * FROM growth_receipts"):
                record = self.growth_record(receipt["record_id"], db)
                if record and self.receipt_current(record, receipt):
                    events[record["id"]] = [e for c in json.loads(receipt["data"])["chunks"] for e in c["events"]]
            items = []
            for row in rows:
                matched = [e for e in events.get(row["record_id"], []) if kind == "all" or e["kind"] == kind]
                if kind != "all" and not matched:
                    continue
                index = row["body"].lower().find(query.lower()) if query else 0
                start = max(0, index - 50)
                items.append({k: row[k] for k in ("record_id", "period", "title", "created_at") } |
                             {"preview": ("…" if start else "") + row["body"][start:start + 220], "events": matched[:3]})
            # Exact diary dates sort by occurrence; fuzzy experience periods stay explicitly fuzzy.
            items.sort(key=lambda r: (r["period"] if len(r["period"]) >= 7 and r["period"][:4].isdigit() else r["created_at"], r["created_at"]), reverse=True)
            return {"items": items[offset:offset + 30], "total": len(items), "offset": offset, "query": query, "kind": kind}


def execute_growth(worker, job, settings):
    from app.prompts import GROWTH_PROMPT
    from app.request_context import prepare_request
    store = worker.store
    with store.connection() as db:
        record = store.growth_record(job["target"], db)
        if not record or record["revision"] != job["revision"] or not settings["growth_enabled"] or settings["growth_paused"]:
            return
        receipt = db.execute("SELECT * FROM growth_receipts WHERE record_id=?", (record["id"],)).fetchone()
        index = receipt["next_chunk"] if store.receipt_current(record, receipt) else 0
        parts = chunks(record)
        if index >= len(parts):
            return
        part = {**record, "body": parts[index]}
        decisions = [{"title": m["title"], "detail": m["correction"] if m["status"] == "corrected" else m["detail"]}
                     for m in store.memories(db) if m["user_modified"] and any(s["record_id"] == record["id"] for s in m["sources"])][:6]
    payload, decode = prepare_request("growth", {"records": [part], "user_decisions": decisions, "part": index + 1, "parts": len(parts)})
    result = validate_growth(decode(worker.client.call(settings, GROWTH_PROMPT, payload)), part)
    with store.connection() as db:
        latest = store.growth_record(job["target"], db)
        if not latest or record_hash(latest) != record_hash(record) or not db.execute("SELECT 1 FROM jobs WHERE id=?", (job["id"],)).fetchone():
            return
        store.save_growth_chunk(db, record, result, settings["ai_model"], index, len(parts))
        if index + 1 < len(parts):
            store.enqueue(db, "growth", record["id"], record["revision"])
