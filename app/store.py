"""Transactional local diary storage. All timestamps are UTC; journal days are explicit."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone, timedelta, date
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def uid():
    return str(uuid.uuid4())


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def day_valid(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("请选择有效日期")
    return value


def tzinfo(name):
    # Windows Python does not necessarily ship the IANA database.
    fixed = {"Asia/Shanghai": 8, "Asia/Tokyo": 9, "Asia/Singapore": 8, "UTC": 0}
    if name in fixed:
        return timezone(timedelta(hours=fixed[name]))
    raise ValueError("暂支持上海、东京、新加坡及 UTC 时区")


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entries(
 id TEXT PRIMARY KEY,body TEXT NOT NULL,journal_day TEXT NOT NULL,timezone TEXT NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,revision INTEGER NOT NULL,
 ai_enabled INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS entries_day ON entries(journal_day,created_at);
CREATE TABLE IF NOT EXISTS entry_versions(
 entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,revision INTEGER NOT NULL,
 body TEXT NOT NULL,journal_day TEXT NOT NULL,timezone TEXT NOT NULL,ai_enabled INTEGER NOT NULL,
 saved_at TEXT NOT NULL,PRIMARY KEY(entry_id,revision));
CREATE TABLE IF NOT EXISTS analyses(
 entry_id TEXT PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL,data TEXT NOT NULL,model TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actions(
 id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL UNIQUE,title TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('task','opportunity','question')),
 status TEXT NOT NULL CHECK(status IN ('open','done','cancelled','dismissed')),
 due_text TEXT,due_date TEXT,user_modified INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS action_sources(
 action_id TEXT NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
 entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL,quote TEXT NOT NULL,
 PRIMARY KEY(action_id,entry_id));
CREATE TABLE IF NOT EXISTS action_history(
 id TEXT PRIMARY KEY,action_id TEXT NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
 before_data TEXT NOT NULL,after_data TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS daily_summaries(
 day TEXT PRIMARY KEY,signature TEXT NOT NULL,data TEXT NOT NULL,model TEXT NOT NULL,
 updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY,kind TEXT NOT NULL,target TEXT NOT NULL,revision INTEGER NOT NULL,
 status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,error TEXT,
 retry_at TEXT,updated_at TEXT NOT NULL,UNIQUE(kind,target));
PRAGMA user_version=1;
"""

DEFAULTS = {"timezone": "Asia/Shanghai", "ai_mode": "off", "ai_base_url": "",
            "ai_model": "", "cloud_consent": False, "backup_keep": 30,
            "backup_dir": "", "last_backup_at": "", "backup_error": ""}


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "journal.sqlite3"
        self.lock = threading.RLock()
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError("数据库来自更新的应用版本，请使用较新程序打开")
            db.executescript(SCHEMA)
            for k, v in DEFAULTS.items():
                db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, compact(v)))
            db.execute("UPDATE jobs SET status='queued' WHERE status='running'")

    @contextlib.contextmanager
    def connection(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            try:
                with db:
                    yield db
            finally:
                db.close()

    def settings(self, db=None):
        if db is None:
            with self.connection() as c:
                return self.settings(c)
        return {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM settings")}

    def save_settings(self, values):
        allowed = set(DEFAULTS) - {"last_backup_at", "backup_error"}
        with self.connection() as db:
            for k, v in values.items():
                if k in allowed:
                    db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, compact(v)))
            db.execute("UPDATE jobs SET status='queued',attempts=0,retry_at=NULL,error=NULL WHERE status IN ('waiting','failed')")

    @staticmethod
    def enqueue(db, kind, target, revision=0):
        db.execute("""INSERT INTO jobs VALUES(?,?,?,?,?,0,NULL,NULL,?)
           ON CONFLICT(kind,target) DO UPDATE SET id=excluded.id,revision=excluded.revision,
           status='queued',attempts=0,error=NULL,retry_at=NULL,updated_at=excluded.updated_at""",
                   (uid(), kind, target, revision, "queued", now()))

    def today(self):
        return datetime.now(tzinfo(self.settings()["timezone"])).date().isoformat()

    def save_entry(self, payload, entry_id=None):
        body = payload.get("body", "")
        if not isinstance(body, str) or not body.strip() or len(body) > 60000:
            raise ValueError("请输入 1–60,000 个字符的记录")
        day = day_valid(payload.get("journal_day", self.today()))
        enabled = bool(payload.get("ai_enabled", True))
        with self.connection() as db:
            previous = db.execute("SELECT * FROM entries WHERE id=?", (entry_id or payload.get("id", ""),)).fetchone()
            if entry_id and not previous:
                raise ValueError("记录已不存在，请刷新后重试")
            if not entry_id and previous:
                if previous["body"] != body.strip() or previous["journal_day"] != day:
                    raise ValueError("保存请求重复且内容发生变化，请刷新确认已保存的记录")
                return dict(previous)
            if previous and payload.get("revision") != previous["revision"]:
                raise ValueError("记录已在其他窗口更新，请重新打开后编辑")
            identifier = previous["id"] if previous else payload.get("id", uid())
            if not isinstance(identifier, str) or len(identifier) > 80:
                raise ValueError("无效的记录编号")
            revision = previous["revision"] + 1 if previous else 1
            stamp = now()
            zone = previous["timezone"] if previous else self.settings(db)["timezone"]
            db.execute("""INSERT INTO entries VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
              body=excluded.body,journal_day=excluded.journal_day,updated_at=excluded.updated_at,
              revision=excluded.revision,ai_enabled=excluded.ai_enabled""",
                       (identifier, body.strip(), day, zone, previous["created_at"] if previous else stamp, stamp, revision, enabled))
            db.execute("INSERT INTO entry_versions VALUES(?,?,?,?,?,?,?)",
                       (identifier, revision, body.strip(), day, zone, enabled, stamp))
            db.execute("DELETE FROM analyses WHERE entry_id=?", (identifier,))
            db.execute("DELETE FROM daily_summaries WHERE day=?", (day,))
            if previous:
                db.execute("DELETE FROM daily_summaries WHERE day=?", (previous["journal_day"],))
            # Do not leave AI claims tied to an obsolete version of the text.
            db.execute("DELETE FROM action_sources WHERE entry_id=?", (identifier,))
            self.clean_orphan_actions(db)
            db.execute("DELETE FROM jobs WHERE kind='entry' AND target=?", (identifier,))
            if enabled:
                self.enqueue(db, "entry", identifier, revision)
            self.enqueue(db, "day", day)
            if previous and previous["journal_day"] != day:
                self.enqueue(db, "day", previous["journal_day"])
            return dict(db.execute("SELECT * FROM entries WHERE id=?", (identifier,)).fetchone())

    @staticmethod
    def clean_orphan_actions(db):
        # Retain manual changes and rejected suggestions to respect the user's choices.
        db.execute("""DELETE FROM actions WHERE user_modified=0 AND status='open'
          AND NOT EXISTS(SELECT 1 FROM action_sources s WHERE s.action_id=actions.id)""")

    def delete_entry(self, identifier):
        with self.connection() as db:
            row = db.execute("SELECT journal_day FROM entries WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise ValueError("记录已不存在")
            db.execute("DELETE FROM entries WHERE id=?", (identifier,))
            db.execute("DELETE FROM daily_summaries WHERE day=?", (row[0],))
            db.execute("DELETE FROM jobs WHERE kind='entry' AND target=?", (identifier,))
            self.clean_orphan_actions(db)
            self.enqueue(db, "day", row[0])

    def entries(self, day=None, query=""):
        with self.connection() as db:
            sql = """SELECT e.*,a.data AS analysis,a.updated_at AS analyzed_at,
              a.revision AS analyzed_revision,j.status AS job_status,j.error AS analysis_error
              FROM entries e LEFT JOIN analyses a ON a.entry_id=e.id
              LEFT JOIN jobs j ON j.kind='entry' AND j.target=e.id WHERE 1=1"""
            args = []
            if day:
                sql += " AND e.journal_day=?"
                args.append(day_valid(day))
            if query:
                sql += " AND (instr(lower(e.body),lower(?))>0 OR instr(lower(coalesce(a.data,'')),lower(?))>0)"
                args.extend([query, query])
            sql += " ORDER BY e.journal_day DESC,e.created_at DESC" + (" LIMIT 500" if not day else "")
            result = []
            for r in db.execute(sql, args):
                item = dict(r)
                item["analysis"] = json.loads(item["analysis"]) if item["analysis"] else None
                result.append(item)
            return result

    def calendar(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT journal_day AS day,count(*) AS count FROM entries GROUP BY journal_day ORDER BY journal_day DESC")]

    def actions(self):
        with self.connection() as db:
            result = []
            for r in db.execute("SELECT * FROM actions ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,created_at DESC"):
                item = dict(r)
                item["sources"] = [dict(s) for s in db.execute("""SELECT s.*,e.journal_day
                   FROM action_sources s JOIN entries e ON e.id=s.entry_id WHERE action_id=?""", (r["id"],))]
                result.append(item)
            return result

    def update_action(self, identifier, payload):
        with self.connection() as db:
            before = db.execute("SELECT * FROM actions WHERE id=?", (identifier,)).fetchone()
            if not before:
                raise ValueError("行动已不存在")
            after = dict(before)
            for k in ("title", "kind", "status", "due_text", "due_date"):
                if k in payload:
                    after[k] = payload[k]
            if not isinstance(after["title"], str) or not after["title"].strip() or len(after["title"]) > 300:
                raise ValueError("行动标题需为 1–300 个字符")
            if after["kind"] not in ("task", "opportunity", "question") or after["status"] not in ("open", "done", "cancelled", "dismissed"):
                raise ValueError("无效的行动状态")
            if after["due_date"]:
                day_valid(after["due_date"])
            after["user_modified"] = 1
            after["updated_at"] = now()
            db.execute("UPDATE actions SET title=?,kind=?,status=?,due_text=?,due_date=?,user_modified=1,updated_at=? WHERE id=?",
                       tuple(after[k] for k in ("title", "kind", "status", "due_text", "due_date", "updated_at")) + (identifier,))
            history = uid()
            db.execute("INSERT INTO action_history VALUES(?,?,?,?,?)", (history, identifier, compact(dict(before)), compact(after), now()))
            for r in db.execute("SELECT DISTINCT e.journal_day FROM entries e JOIN action_sources s ON s.entry_id=e.id WHERE s.action_id=?", (identifier,)):
                self.enqueue(db, "day", r[0])
            return history

    def undo_action(self, history):
        with self.connection() as db:
            change = db.execute("SELECT * FROM action_history WHERE id=?", (history,)).fetchone()
            if not change:
                raise ValueError("此变更已撤销")
            current = db.execute("SELECT * FROM actions WHERE id=?", (change["action_id"],)).fetchone()
            if not current or compact(dict(current)) != change["after_data"]:
                raise ValueError("行动已有后续修改，请先查看当前状态")
            value = json.loads(change["before_data"])
            # Undo remains a deliberate user choice, protected from later model updates.
            db.execute("UPDATE actions SET title=?,kind=?,status=?,due_text=?,due_date=?,user_modified=1,updated_at=? WHERE id=?",
                       tuple(value[k] for k in ("title", "kind", "status", "due_text", "due_date")) + (now(), value["id"]))
            db.execute("DELETE FROM action_history WHERE id=?", (history,))
            for r in db.execute("SELECT DISTINCT e.journal_day FROM entries e JOIN action_sources s ON e.id=s.entry_id WHERE action_id=?", (value["id"],)):
                self.enqueue(db, "day", r[0])

    def day_context(self, day):
        records = [e for e in self.entries(day) if e["ai_enabled"]]
        ids = {e["id"] for e in records}
        actions = [a for a in self.actions() if any(s["entry_id"] in ids for s in a["sources"])]
        context = {"day": day, "records": [{k: e[k] for k in ("id", "body", "revision", "created_at", "timezone", "analysis")} for e in records],
                   "actions": [{k: a[k] for k in ("id", "title", "kind", "status", "sources")} for a in actions]}
        signature = hashlib.sha256(compact(context).encode()).hexdigest()
        return context, signature

    def summary(self, day):
        _, signature = self.day_context(day)
        with self.connection() as db:
            row = db.execute("SELECT * FROM daily_summaries WHERE day=?", (day,)).fetchone()
            job = db.execute("SELECT status,error FROM jobs WHERE kind='day' AND target=?", (day,)).fetchone()
            result = dict(row) if row else {"day": day, "data": None}
            if row:
                result["data"] = json.loads(row["data"])
            result["stale"] = bool(row and row["signature"] != signature)
            result["status"] = job["status"] if job else "empty"
            result["error"] = job["error"] if job else None
            return result

    def retry(self, entry_id=None, day=None):
        with self.connection() as db:
            if entry_id:
                row = db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
                if not row or not row["ai_enabled"]:
                    raise ValueError("该记录不存在或已排除 AI 分析")
                self.enqueue(db, "entry", entry_id, row["revision"])
                self.enqueue(db, "day", row["journal_day"])
            if day:
                self.enqueue(db, "day", day_valid(day))

    def export(self):
        with self.connection() as db:
            tables = {table: [dict(r) for r in db.execute("SELECT * FROM " + table)] for table in
                      ("entries", "entry_versions", "analyses", "actions", "action_sources", "action_history", "daily_summaries")}
            return {"format": "shiguang-diary", "schema_version": 1, "exported_at": now(), "tables": tables,
                    "preferences": {k: v for k, v in self.settings(db).items() if k in ("timezone", "backup_keep")}}

    def validate_import(self, data):
        if not isinstance(data, dict) or data.get("format") != "shiguang-diary" or data.get("schema_version") != 1:
            raise ValueError("不是受支持的拾光日记备份（格式版本 1）")
        tables = data.get("tables")
        expected = ("entries", "entry_versions", "analyses", "actions", "action_sources", "action_history", "daily_summaries")
        if not isinstance(tables, dict) or set(tables) != set(expected):
            raise ValueError("备份表结构不完整")
        # Validate in a scratch database: real constraints, types and references must hold.
        scratch = sqlite3.connect(":memory:")
        try:
            scratch.execute("PRAGMA foreign_keys=ON")
            scratch.executescript(SCHEMA)
            for table in expected:
                columns = [r[1] for r in scratch.execute("PRAGMA table_info(" + table + ")")]
                rows = tables[table]
                if not isinstance(rows, list) or len(rows) > 100000:
                    raise ValueError("备份数据数量无效")
                for row in rows:
                    if not isinstance(row, dict) or set(row) != set(columns):
                        raise ValueError("备份字段不匹配：" + table)
                    scratch.execute("INSERT INTO " + table + " VALUES(" + ",".join("?" for _ in columns) + ")", [row[c] for c in columns])
            for e in tables["entries"]:
                day_valid(e["journal_day"])
                tzinfo(e["timezone"])
                if not isinstance(e["body"], str) or not e["body"].strip() or len(e["body"]) > 60000 or not isinstance(e["revision"], int) or e["revision"] < 1:
                    raise ValueError("备份包含无效记录")
                datetime.fromisoformat(e["created_at"])
                datetime.fromisoformat(e["updated_at"])
            from app.ai import validate_entry, validate_day, AIError
            entry_map = {e["id"]: e for e in tables["entries"]}
            for v in tables["entry_versions"]:
                day_valid(v["journal_day"])
                tzinfo(v["timezone"])
                if not isinstance(v["body"], str) or not v["body"].strip() or len(v["body"]) > 60000:
                    raise ValueError("备份历史原文无效")
            versions = {(v["entry_id"], v["revision"]): v["body"] for v in tables["entry_versions"]}
            for e in tables["entries"]:
                if versions.get((e["id"], e["revision"])) != e["body"]:
                    raise ValueError("备份缺少与当前记录一致的原文版本")
            for action in tables["actions"]:
                if not isinstance(action["title"], str) or not action["title"].strip() or len(action["title"]) > 300:
                    raise ValueError("备份行动内容无效")
                if action["due_date"]:
                    day_valid(action["due_date"])
            for source in tables["action_sources"]:
                e = entry_map[source["entry_id"]]
                if source["revision"] != e["revision"] or not isinstance(source["quote"], str) or not source["quote"] or source["quote"] not in e["body"]:
                    raise ValueError("备份行动来源与原文不一致")
            for a in tables["analyses"]:
                e = entry_map[a["entry_id"]]
                if a["revision"] != e["revision"]:
                    raise ValueError("备份分析版本过期")
                # Schema and quote validation prevent corrupt imports from breaking the UI.
                try:
                    validate_entry(json.loads(a["data"]), e, tables["actions"])
                except AIError as exc:
                    raise ValueError("备份分析内容不符合当前格式") from exc
            for s in tables["daily_summaries"]:
                day_valid(s["day"])
                try:
                    validate_day(json.loads(s["data"]), {"records": list(entry_map.values())})
                except AIError as exc:
                    raise ValueError("备份每日整理不符合当前格式") from exc
            for h in tables["action_history"]:
                json.loads(h["before_data"])
                json.loads(h["after_data"])
        except (sqlite3.Error, TypeError, KeyError, AttributeError, UnboundLocalError) as exc:
            raise ValueError("备份数据损坏或关联不完整") from exc
        finally:
            scratch.close()
        with self.connection() as db:
            conflicts = 0
            duplicate = 0
            for e in tables["entries"]:
                current = db.execute("SELECT * FROM entries WHERE id=?", (e["id"],)).fetchone()
                if current:
                    if dict(current) == e:
                        duplicate += 1
                    else:
                        conflicts += 1
            return {"entries": len(tables["entries"]), "new": len(tables["entries"]) - duplicate - conflicts,
                    "duplicates": duplicate, "conflicts": conflicts, "actions": len(tables["actions"])}

    def import_data(self, data, replace=False):
        preview = self.validate_import(data)
        self.backup(force=True, label="before-import")
        tables = data["tables"]
        with self.connection() as db:
            if replace:
                for table in ("jobs", "daily_summaries", "action_history", "action_sources", "actions", "analyses", "entry_versions", "entries"):
                    db.execute("DELETE FROM " + table)
            # Merge deliberately preserves local conflicts, including task user overrides.
            skip_ids = set()
            for row in tables["entries"]:
                if db.execute("SELECT 1 FROM entries WHERE id=?", (row["id"],)).fetchone():
                    skip_ids.add(row["id"])
            action_map = {}
            for table in ("entries", "entry_versions", "analyses", "actions", "action_sources", "action_history", "daily_summaries"):
                rows = tables[table]
                for row in rows:
                    row = dict(row)
                    if table in ("entry_versions", "analyses", "action_sources") and row["entry_id"] in skip_ids:
                        continue
                    if table == "actions":
                        present = db.execute("SELECT id FROM actions WHERE id=? OR fingerprint=? ORDER BY id=? DESC LIMIT 1", (row["id"], row["fingerprint"], row["id"])).fetchone()
                        if present:
                            action_map[row["id"]] = present["id"]
                            continue
                        action_map[row["id"]] = row["id"]
                    if table in ("action_sources", "action_history"):
                        original_action = row["action_id"]
                        row["action_id"] = action_map.get(original_action, original_action)
                        if table == "action_history" and original_action != row["action_id"]:
                            continue
                    columns = list(row)
                    db.execute("INSERT OR IGNORE INTO " + table + "(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", list(row.values()))
            if replace:
                for k, v in data.get("preferences", {}).items():
                    if k == "timezone":
                        tzinfo(v)
                        db.execute("UPDATE settings SET value=? WHERE key=?", (compact(v), k))
            for r in db.execute("SELECT * FROM entries WHERE ai_enabled=1 AND NOT EXISTS(SELECT 1 FROM analyses WHERE entry_id=entries.id AND revision=entries.revision)").fetchall():
                self.enqueue(db, "entry", r["id"], r["revision"])
            for r in db.execute("SELECT DISTINCT journal_day FROM entries").fetchall():
                self.enqueue(db, "day", r[0])
        return preview

    def backup(self, force=False, label="auto"):
        with self.lock:
            settings = self.settings()
            today = self.today()
            if not force and settings["last_backup_at"].startswith(today):
                return None
            folder = Path(settings["backup_dir"] or self.directory / "backups").expanduser().resolve()
            folder.mkdir(parents=True, exist_ok=True)
            name = "shiguang-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f") + "-" + label + ".sqlite3"
            destination = folder / name
            temporary = folder / (name + ".partial")
            try:
                source = sqlite3.connect(self.path)
                target = sqlite3.connect(temporary)
                try:
                    source.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("备份完整性检查未通过")
                finally:
                    target.close()
                    source.close()
                os.replace(temporary, destination)
                with self.connection() as db:
                    db.execute("UPDATE settings SET value=? WHERE key='last_backup_at'", (compact(today + " " + now()),))
                    db.execute("UPDATE settings SET value=? WHERE key='backup_error'", (compact(""),))
                existing = sorted(folder.glob("shiguang-*.sqlite3"), reverse=True)
                for stale in existing[int(settings["backup_keep"]):]:
                    stale.unlink()
                return name
            except Exception:
                with self.connection() as db:
                    db.execute("UPDATE settings SET value=? WHERE key='backup_error'", (compact("自动备份失败，请检查目录权限和磁盘空间"),))
                raise
            finally:
                temporary.unlink(missing_ok=True)

    def backups(self):
        settings = self.settings()
        folder = Path(settings["backup_dir"] or self.directory / "backups").resolve()
        return [{"name": p.name, "size": p.stat().st_size} for p in sorted(folder.glob("shiguang-*.sqlite3"), reverse=True)]

    def restore_backup(self, name):
        if not isinstance(name, str) or Path(name).name != name or name not in {b["name"] for b in self.backups()}:
            raise ValueError("请选择列表中的备份")
        folder = Path(self.settings()["backup_dir"] or self.directory / "backups")
        source = sqlite3.connect("file:" + (folder / name).resolve().as_posix() + "?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        try:
            if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("备份未通过完整性检查")
            bundle = {"format": "shiguang-diary", "schema_version": 1, "tables": {t: [dict(r) for r in source.execute("SELECT * FROM " + t)] for t in self.export()["tables"]}}
            return self.import_data(bundle, replace=True)
        finally:
            source.close()
