"""Opt-in model adapter, evidence validation and persistent background jobs."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import ipaddress
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from app.store import compact, day_valid, now, uid, tzinfo
from app.prompts import ENTRY_PROMPT, DAY_PROMPT
from app.action_dates import duration_deadline
from app.usage import call_scope, usage_counts

REQUEST_OPTIONS = {"temperature": 0.2, "stream": False}


class AIError(Exception):
    pass


def dpapi(data, decrypt=False):
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.windll.crypt32
    name = "CryptUnprotectData" if decrypt else "CryptProtectData"
    fn = getattr(crypt, name)
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise AIError("无法访问当前 Windows 用户的密钥存储")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


class KeyStore:
    def __init__(self, directory):
        self.path = directory / "ai-key.dat"

    def set(self, value):
        if not isinstance(value, str) or len(value) > 4000 or any(ord(c) < 32 for c in value):
            raise ValueError("密钥格式无效")
        value = value.strip()
        if not value:
            self.path.unlink(missing_ok=True)
            return
        encoded = b"DP1:" + base64.b64encode(dpapi(value.encode())) if os.name == "nt" else b"RAW:" + value.encode()
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(encoded)
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def get(self):
        if not self.path.exists():
            return ""
        raw = self.path.read_bytes()
        if raw.startswith(b"DP1:"):
            return dpapi(base64.b64decode(raw[4:]), decrypt=True).decode()
        return raw[4:].decode()


def validate_config(settings):
    mode = settings.get("ai_mode")
    if mode not in ("off", "local", "cloud"):
        raise ValueError("请选择有效的 AI 模式")
    if mode == "off":
        return
    url = urllib.parse.urlsplit(settings.get("ai_base_url", "").strip())
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("请填写不包含凭证、查询参数或锚点的服务地址")
    if mode == "local":
        is_local = url.hostname == "localhost"
        try:
            is_local = is_local or ipaddress.ip_address(url.hostname).is_loopback
        except ValueError:
            pass
        if not is_local or url.scheme not in ("http", "https"):
            raise ValueError("本地模式仅支持当前电脑的 localhost 或回环 IP 地址")
    elif url.scheme != "https" or not settings.get("cloud_consent"):
        raise ValueError("云端服务需要 HTTPS 地址，并确认允许发送待分析日记")
    if not isinstance(settings.get("ai_model"), str) or not settings["ai_model"].strip() or len(settings["ai_model"]) > 200:
        raise ValueError("请填写模型名称")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise AIError("AI 地址发生重定向，请直接配置最终服务地址")


class ModelClient:
    def __init__(self, keys, ledger=None):
        self.keys, self.ledger = keys, ledger

    def call(self, settings, system, content):
        return self.call_with_metadata(settings, system, content)[0]

    def call_with_metadata(self, settings, system, content):
        """One request, with usage captured before content parsing can fail."""
        validate_config(settings)
        if settings["ai_mode"] == "off":
            raise AIError("AI 尚未启用")
        base = settings["ai_base_url"].rstrip("/")
        endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
        payload = {"model": settings["ai_model"], **REQUEST_OPTIONS,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": compact(content)}],
                   }
        key = self.keys.get()
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(endpoint, compact(payload).encode(), headers=headers, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        identifier = None
        if self.ledger:
            try:
                identifier = self.ledger.start(settings)
            except Exception:
                raise AIError("无法创建本次用量记录，已暂停模型调用，请检查数据目录与磁盘空间") from None
        started = time.perf_counter()
        raw_usage, returned_model = {}, None
        status, error_code = "error", "invalid_response"
        try:
            with opener.open(request, timeout=75) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise AIError("AI 响应过大，请使用更简洁的模型输出")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise AIError("AI 返回了不支持的内容格式")
                raw_usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
                returned_model = result.get("model")
                answer = result["choices"][0]["message"]["content"]
                if not isinstance(answer, str):
                    raise AIError("AI 返回了不支持的内容格式")
                answer = re.sub(r"^```(?:json)?\s*|\s*```$", "", answer.strip())
                output = json.loads(answer)
                counts = usage_counts(raw_usage)
                metadata = {"requested_model": settings["ai_model"], "returned_model": returned_model,
                            "usage": {"prompt_tokens": counts["input_tokens"], "completion_tokens": counts["output_tokens"],
                                      "total_tokens": counts["total_tokens"], "cached_tokens": counts["cached_tokens"],
                                      "reasoning_tokens": counts["reasoning_tokens"]}}
                status, error_code = "success", None
                return output, metadata
        except urllib.error.HTTPError as exc:
            error_code = "http_" + str(exc.code)
            messages = {401: "AI 密钥无效或未授权", 403: "AI 服务拒绝访问", 404: "找不到模型或接口，请检查地址与模型名", 429: "AI 服务限流或额度不足，请稍后重试"}
            raise AIError(messages.get(exc.code, "AI 服务暂时出错（HTTP %s）" % exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            error_code = "network_or_timeout"
            raise AIError("无法连接 AI 服务或请求超时；日记已保存在本机") from None
        except (KeyError, IndexError, json.JSONDecodeError, TypeError):
            raise AIError("AI 未返回有效的结构化结果，可更换模型或重试") from None
        finally:
            if identifier:
                try:
                    self.ledger.finish(identifier, raw_usage, returned_model, status,
                                       round((time.perf_counter() - started) * 1000), error_code)
                except Exception:
                    # Never retry a paid response merely because accounting failed.
                    self.ledger.warning = "部分用量记录未能写回，费用可能不完整；请检查磁盘空间。"
                else:
                    try:
                        self.ledger.backup(settings)
                    except Exception:
                        self.ledger.warning = "用量已记录，但独立快照备份失败；请检查备份目录与磁盘空间。"


def text(value, maximum=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise AIError("AI 输出字段不符合约定")
    return value.strip()


def array(data, key, limit=20):
    value = data.get(key)
    if not isinstance(value, list) or len(value) > limit:
        raise AIError("AI 输出列表不符合约定")
    return value


def evidence(quote, body):
    quote = text(quote, 4000)
    if quote not in body:
        raise AIError("AI 引用的内容无法在原文中找到，已拒绝本次结果")
    return quote


def validate_entry(data, entry, existing):
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise AIError("AI 输出版本无效")
    output = {"schema_version": 1, "summary": text(data.get("summary"), 800), "insights": [], "actions": [], "completions": []}
    known = {a["id"]: a for a in existing}
    for item in array(data, "insights"):
        kind = item.get("type")
        if kind not in ("fact", "idea"):
            raise AIError("AI 结论类型无效")
        output["insights"].append({"text": text(item.get("text")), "quote": evidence(item.get("quote"), entry["body"]), "type": kind})
    for item in array(data, "actions"):
        kind = item.get("kind")
        if kind not in ("task", "opportunity", "question"):
            raise AIError("AI 行动类型无效")
        quote = evidence(item.get("quote"), entry["body"])
        title = text(item.get("title"), 300)
        # Conservative guard for the most common false-positive commitments.
        if kind == "task" and re.search(r"不准备|不打算|不需要|不用|已(?:经)?完成|已联系|已经联系|只是举例|例如|别人(?:说|计划)|他(?:说|计划)", quote):
            continue
        if kind == "task" and re.search(r"也许|或许|可能值得|可以考虑|不确定是否", quote):
            kind = "opportunity"
        due_text = item.get("due_text")
        due_date = item.get("due_date")
        if due_text is not None:
            due_text = text(due_text, 200)
            if due_text not in entry["body"]:
                raise AIError("AI 截止时间缺少原文依据")
        if due_date is not None:
            if not due_text:
                raise AIError("AI 日期缺少原文依据")
            day_valid(due_date)
        resolved = duration_deadline(entry, quote, kind)
        if resolved:
            due_text, due_date = resolved
        existing_id = item.get("existing_action_id")
        if existing_id and existing_id not in known:
            raise AIError("AI 引用了不存在的行动")
        output["actions"].append({"title": title, "kind": kind, "quote": quote, "due_text": due_text, "due_date": due_date, "existing_action_id": existing_id})
    for item in array(data, "completions"):
        identifier = item.get("action_id")
        quote = evidence(item.get("quote"), entry["body"])
        if identifier not in known or not re.search(r"完成|做完|办好|发完|已发送|已经.*(?:联系|发送|提交)", quote) or re.search(r"没完成|未完成|还没|尚未", quote):
            continue
        output["completions"].append({"action_id": identifier, "quote": quote})
    return output


def validate_day(data, context):
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise AIError("每日整理格式无效")
    records = {e["id"]: e for e in context["records"]}
    output = {"schema_version": 1, "overview": text(data.get("overview"), 1000), "sections": []}
    seen = set()
    for section in array(data, "sections", 5):
        category = section.get("category")
        if category not in ("events", "insights", "decisions", "actions", "opportunities") or category in seen:
            raise AIError("每日整理类别无效")
        seen.add(category)
        items = []
        for item in array(section, "items", 12):
            sources = []
            for source in array(item, "sources", 20):
                identifier = source.get("entry_id")
                if identifier not in records:
                    raise AIError("每日整理引用了不存在的记录")
                sources.append({"entry_id": identifier, "quote": evidence(source.get("quote"), records[identifier]["body"])})
            if not sources:
                raise AIError("每日整理缺少来源")
            items.append({"text": text(item.get("text")), "sources": sources})
        if items:
            output["sections"].append({"category": category, "items": items})
    return output


class Worker:
    def __init__(self, store, client):
        self.store, self.client = store, client
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="diary-analysis")

    def start(self):
        self.thread.start()

    def run(self):
        while not self.stop.wait(1):
            try:
                self.step()
            except Exception:
                # Never log diary text, payloads, responses or credentials.
                pass

    def step(self):
        store = self.store
        with store.connection() as db:
            job = db.execute("SELECT * FROM jobs WHERE status='queued' AND (retry_at IS NULL OR retry_at<=?) ORDER BY CASE kind WHEN 'entry' THEN 0 ELSE 1 END,updated_at LIMIT 1", (now(),)).fetchone()
            if not job:
                return False
            job = dict(job)
            settings = store.settings(db)
            if settings["ai_mode"] == "off":
                db.execute("UPDATE jobs SET status='waiting',error='请在设置中配置 AI 服务' WHERE id=?", (job["id"],))
                return True
            db.execute("UPDATE jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=?", (now(), job["id"]))
        try:
            with call_scope(job["kind"], job["target"], job["id"], job["attempts"] + 1):
                if job["kind"] == "entry":
                    self.analyze_entry(job, settings)
                elif job["kind"] in ("memory", "portrait", "month"):
                    from app.about_ai import execute_about
                    execute_about(self, job, settings)
                else:
                    self.analyze_day(job, settings)
            with store.connection() as db:
                db.execute("UPDATE jobs SET status='done',error=NULL,updated_at=? WHERE id=?", (now(), job["id"]))
            try:
                store.backup()
            except Exception:
                pass  # Backup status is reported separately from analysis success.
        except Exception as exc:
            attempts = job["attempts"] + 1
            message = str(exc) if isinstance(exc, (AIError, ValueError)) else "整理失败，记录仍安全保存在本机，请重试"
            retry_at = (datetime.now(timezone.utc) + timedelta(seconds=10 * attempts)).isoformat(timespec="milliseconds")
            with store.connection() as db:
                db.execute("UPDATE jobs SET status=?,error=?,retry_at=?,updated_at=? WHERE id=?",
                           ("failed" if attempts >= (1 if job["kind"] in ("memory", "portrait", "month") else 3) else "queued", message[:300], retry_at, now(), job["id"]))
        return True

    def analyze_entry(self, job, settings):
        store = self.store
        with store.connection() as db:
            row = db.execute("SELECT * FROM entries WHERE id=?", (job["target"],)).fetchone()
            if not row or not row["ai_enabled"] or row["revision"] != job["revision"]:
                return
            entry = dict(row)
        with store.connection() as db:
            enabled_ids = {r[0] for r in db.execute("SELECT id FROM entries WHERE ai_enabled=1")}
        existing = [a for a in store.actions() if a["sources"] and all(s["entry_id"] in enabled_ids for s in a["sources"])][:100]
        context = {"entry": entry, "creation_local_date": datetime.fromisoformat(entry["created_at"]).astimezone(tzinfo(entry["timezone"])).date().isoformat(),
                   "existing_actions": [{k: a[k] for k in ("id", "title", "kind", "status")} for a in existing]}
        result = validate_entry(self.client.call(settings, ENTRY_PROMPT, context), entry, existing)
        with store.connection() as db:
            current = db.execute("SELECT revision,ai_enabled FROM entries WHERE id=?", (entry["id"],)).fetchone()
            ticket = db.execute("SELECT 1 FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if not current or current[0] != entry["revision"] or not current[1] or not ticket:
                return
            db.execute("INSERT OR REPLACE INTO analyses VALUES(?,?,?,?,?)", (entry["id"], entry["revision"], compact(result), settings["ai_model"], now()))
            for action in result["actions"]:
                normalized = re.sub(r"[\W_]", "", action["title"].lower())
                fingerprint = hashlib.sha256((action["kind"] + ":" + normalized).encode()).hexdigest()
                row = db.execute("SELECT * FROM actions WHERE id=? OR fingerprint=? LIMIT 1", (action["existing_action_id"], fingerprint)).fetchone()
                identifier = row["id"] if row else uid()
                if not row:
                    db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?)", (identifier, fingerprint, action["title"], action["kind"], "open", action["due_text"], action["due_date"], 0, now(), now()))
                elif not row["user_modified"] and row["status"] == "open" and action["due_date"] is not None and db.execute(
                    "SELECT 1 FROM action_sources WHERE action_id=? AND entry_id=?", (identifier, entry["id"])
                ).fetchone():
                    # Reanalysis can repair a missed date, but never a user's edit.
                    db.execute("UPDATE actions SET due_text=?,due_date=?,updated_at=? WHERE id=?",
                               (action["due_text"], action["due_date"], now(), identifier))
                db.execute("INSERT OR REPLACE INTO action_sources VALUES(?,?,?,?)", (identifier, entry["id"], entry["revision"], action["quote"]))
            for completed in result["completions"]:
                action = db.execute("SELECT * FROM actions WHERE id=? AND status='open' AND user_modified=0", (completed["action_id"],)).fetchone()
                if action:
                    before = dict(action)
                    after = {**before, "status": "done", "updated_at": now()}
                    db.execute("UPDATE actions SET status='done',updated_at=? WHERE id=?", (after["updated_at"], action["id"]))
                    db.execute("INSERT INTO action_history VALUES(?,?,?,?,?)", (uid(), action["id"], compact(before), compact(after), now()))
                    db.execute("INSERT OR REPLACE INTO action_sources VALUES(?,?,?,?)", (action["id"], entry["id"], entry["revision"], completed["quote"]))
                    for source_day in db.execute("SELECT DISTINCT e.journal_day FROM entries e JOIN action_sources s ON e.id=s.entry_id WHERE s.action_id=?", (action["id"],)).fetchall():
                        store.enqueue(db, "day", source_day[0])
            store.enqueue(db, "day", entry["journal_day"])

    def analyze_day(self, job, settings):
        store = self.store
        context, signature = store.day_context(job["target"])
        if not context["records"]:
            with store.connection() as db:
                db.execute("DELETE FROM daily_summaries WHERE day=?", (job["target"],))
            return
        # Bound transmission instead of silently dropping records from a purported full recap.
        if len(compact(context)) > 120000:
            raise AIError("当天内容较多，超出本版单次整理范围；所有原文仍可查看")
        result = validate_day(self.client.call(settings, DAY_PROMPT, context), context)
        with store.connection() as db:
            _, latest = store.day_context(job["target"])
            if latest != signature or not db.execute("SELECT 1 FROM jobs WHERE id=?", (job["id"],)).fetchone():
                return
            db.execute("INSERT OR REPLACE INTO daily_summaries VALUES(?,?,?,?,?)", (job["target"], signature, compact(result), settings["ai_model"], now()))
