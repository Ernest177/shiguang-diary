"""Opt-in personal history, evidence-backed memories and invalidation lifecycle."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timezone, timedelta

ABOUT_TABLES = ("experiences", "memory_permissions", "memories", "memory_sources", "memory_suppressions", "personal_reports")
ABOUT_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiences(
 id TEXT PRIMARY KEY,title TEXT NOT NULL,period TEXT NOT NULL,chapter TEXT NOT NULL,
 body TEXT NOT NULL,ai_enabled INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_permissions(
 entry_id TEXT PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS memories(
 id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL UNIQUE,title TEXT NOT NULL,detail TEXT NOT NULL,
 category TEXT NOT NULL CHECK(category IN ('fact','value','preference','goal','pattern')),
 horizon TEXT NOT NULL CHECK(horizon IN ('long_term','recent','unknown')),period TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','confirmed','corrected')),
 correction TEXT NOT NULL DEFAULT '',user_modified INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_sources(
 memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
 entry_id TEXT REFERENCES entries(id) ON DELETE CASCADE,
 experience_id TEXT REFERENCES experiences(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL,quote TEXT NOT NULL,
 CHECK((entry_id IS NOT NULL) != (experience_id IS NOT NULL)),
 UNIQUE(memory_id,entry_id),UNIQUE(memory_id,experience_id));
CREATE TABLE IF NOT EXISTS memory_suppressions(
 fingerprint TEXT PRIMARY KEY,source_hashes TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS personal_reports(
 kind TEXT NOT NULL,target TEXT NOT NULL,signature TEXT NOT NULL,data TEXT NOT NULL,
 model TEXT NOT NULL,prompt_version TEXT NOT NULL,prompt_hash TEXT NOT NULL,updated_at TEXT NOT NULL,
 PRIMARY KEY(kind,target));
"""


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def signature(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def fingerprint(category, title):
    return signature([category, re.sub(r"[\W_]", "", title.lower())])


def source_hash(category, source):
    return signature([source["record_id"], source["quote"]])


def month_valid(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError("请选择有效月份")
    date.fromisoformat(value + "-01")
    return value


def field(value, limit, optional=False):
    if not isinstance(value, str) or len(value) > limit or (not optional and not value.strip()):
        raise ValueError("请填写有效内容，或缩短过长的文字")
    return value.strip()


class AboutMixin:
    def invalidate_about(self, db, source_type=None, identifier=None):
        affected = True
        if source_type:
            column = {"entry": "entry_id", "experience": "experience_id"}[source_type]
            affected = bool(db.execute("SELECT 1 FROM memory_sources WHERE " + column + "=? LIMIT 1", (identifier,)).fetchone())
            db.execute("DELETE FROM memories WHERE id IN (SELECT memory_id FROM memory_sources WHERE " + column + "=?)", (identifier,))
            db.execute("DELETE FROM memories WHERE NOT EXISTS(SELECT 1 FROM memory_sources s WHERE s.memory_id=memories.id)")
            record_id = source_type + ":" + identifier
            db.execute("DELETE FROM growth_receipts WHERE record_id=?", (record_id,))
            db.execute("DELETE FROM jobs WHERE kind='growth' AND target=?", (record_id,))
        if source_type and self.settings(db).get("growth_enabled") and not affected:
            # An unrelated/private/new source cannot change an existing portrait.
            db.execute("DELETE FROM personal_reports WHERE kind!='portrait'")
            db.execute("DELETE FROM jobs WHERE kind IN ('memory','month')")
        else:
            db.execute("DELETE FROM personal_reports")
            db.execute("DELETE FROM jobs WHERE kind IN ('memory','portrait','month')")

    def save_experience(self, payload):
        title = field(payload.get("title"), 160)
        body = field(payload.get("body"), 20000)
        period = field(payload.get("period", ""), 80, True)
        chapter = payload.get("chapter", "生活")
        if chapter not in ("成长", "学习", "工作", "关系", "生活", "转折"):
            raise ValueError("请选择有效的人生章节")
        enabled = payload.get("ai_enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("请确认是否允许用于长期记忆")
        supplied_id = payload.get("id") or payload.get("client_id")
        if supplied_id is not None and (not isinstance(supplied_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", supplied_id)):
            raise ValueError("经历编号无效")
        with self.connection() as db:
            old = db.execute("SELECT * FROM experiences WHERE id=?", (supplied_id,)).fetchone()
            if old and not payload.get("id"):
                if (old["title"], old["body"], old["period"], old["chapter"], bool(old["ai_enabled"])) == (title, body, period, chapter, enabled):
                    return dict(old)
                raise ValueError("这段经历已保存，请重新打开后编辑")
            if payload.get("id") and not old:
                raise ValueError("经历已不存在")
            if old and old["revision"] != payload.get("revision"):
                raise ValueError("经历已在其他窗口更新，请重新打开")
            identifier = old["id"] if old else supplied_id or str(uuid.uuid4())
            updated = stamp()
            self.invalidate_about(db, "experience", identifier)
            db.execute("""INSERT INTO experiences VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
              title=excluded.title,period=excluded.period,chapter=excluded.chapter,body=excluded.body,
              ai_enabled=excluded.ai_enabled,revision=excluded.revision,updated_at=excluded.updated_at""",
                       (identifier, title, period, chapter, body, enabled, old["revision"] + 1 if old else 1,
                        old["created_at"] if old else updated, updated))
            self.schedule_growth(db)
            return dict(db.execute("SELECT * FROM experiences WHERE id=?", (identifier,)).fetchone())

    def delete_experience(self, identifier):
        with self.connection() as db:
            self.invalidate_about(db, "experience", identifier)
            db.execute("DELETE FROM experiences WHERE id=?", (identifier,))
            self.schedule_portrait(db)

    def set_memory_permission(self, identifier, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("请确认长期记忆选择")
        with self.connection() as db:
            row = db.execute("SELECT ai_enabled FROM entries WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise ValueError("记录已不存在")
            if enabled and not row[0]:
                raise ValueError("请先在这条记录中允许 AI 整理，再选择用于长期记忆")
            self.invalidate_about(db, "entry", identifier)
            if enabled:
                db.execute("INSERT OR IGNORE INTO memory_permissions VALUES(?)", (identifier,))
                db.execute("DELETE FROM memory_exclusions WHERE entry_id=?", (identifier,))
            else:
                db.execute("DELETE FROM memory_permissions WHERE entry_id=?", (identifier,))
                db.execute("INSERT OR IGNORE INTO memory_exclusions VALUES(?)", (identifier,))
            self.schedule_growth(db)
            self.schedule_portrait(db)

    def about_records(self, db):
        records = []
        automatic = self.settings(db).get("growth_enabled", False)
        for row in db.execute("""SELECT e.* FROM entries e WHERE e.ai_enabled=1 AND
          ((?=1 AND NOT EXISTS(SELECT 1 FROM memory_exclusions x WHERE x.entry_id=e.id)) OR
           (?=0 AND EXISTS(SELECT 1 FROM memory_permissions p WHERE p.entry_id=e.id))) ORDER BY e.id""", (automatic, automatic)):
            records.append({"id": "entry:" + row["id"], "type": "entry", "body": row["body"], "revision": row["revision"],
                            "period": row["journal_day"], "title": row["journal_day"] + "的日记"})
        for row in db.execute("SELECT * FROM experiences WHERE ai_enabled=1 ORDER BY id"):
            records.append({"id": "experience:" + row["id"], "type": "experience", "body": row["body"],
                            "revision": row["revision"], "period": row["period"], "title": row["title"]})
        return records

    def memory_context(self, db=None):
        if db is None:
            with self.connection() as db:
                return self.memory_context(db)
        context = {"records": self.about_records(db),
                   "user_decisions": [{"title": m["title"], "category": m["category"], "status": m["status"],
                                       "correction": m["correction"]} for m in self.memories(db) if m["user_modified"]],
                   "suppressed": [dict(r) for r in db.execute("SELECT * FROM memory_suppressions ORDER BY fingerprint")]}
        return context, signature(context)

    def memories(self, db=None):
        if db is None:
            with self.connection() as db:
                return self.memories(db)
        records = {r["id"]: r for r in self.about_records(db)}
        result = []
        for row in db.execute("SELECT * FROM memories ORDER BY created_at DESC,id"):
            memory = dict(row)
            sources = []
            for source in db.execute("SELECT * FROM memory_sources WHERE memory_id=? ORDER BY entry_id,experience_id", (row["id"],)):
                record_id = ("entry:" + source["entry_id"]) if source["entry_id"] else ("experience:" + source["experience_id"])
                record = records.get(record_id)
                if record and record["revision"] == source["revision"] and source["quote"] in record["body"]:
                    sources.append({"record_id": record_id, "quote": source["quote"], "revision": source["revision"],
                                    "title": record["title"], "period": record["period"]})
            if sources:
                memory["sources"] = sources
                result.append(memory)
        return result

    def update_memory(self, identifier, payload):
        with self.connection() as db:
            row = db.execute("SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise ValueError("记忆已不存在或来源已更新")
            if payload.get("updated_at") != row["updated_at"]:
                raise ValueError("记忆已更新，请重新打开后修改")
            status = payload.get("status")
            if status not in ("confirmed", "corrected", "pending"):
                raise ValueError("请选择有效的记忆状态")
            correction = field(payload.get("correction", row["correction"]), 3000, status != "corrected")
            if status != "corrected":
                correction = ""
            self.invalidate_about(db)
            updated = stamp()
            if updated <= row["updated_at"]:
                updated = (datetime.fromisoformat(row["updated_at"]) + timedelta(milliseconds=1)).isoformat(timespec="milliseconds")
            db.execute("UPDATE memories SET status=?,correction=?,user_modified=1,updated_at=? WHERE id=?",
                       (status, correction, updated, identifier))
            self.schedule_portrait(db)

    def forget_memory(self, identifier):
        with self.connection() as db:
            memory = next((m for m in self.memories(db) if m["id"] == identifier), None)
            if not memory:
                raise ValueError("记忆已不存在")
            hashes = [source_hash(memory["category"], s) for s in memory["sources"]]
            old = db.execute("SELECT source_hashes FROM memory_suppressions WHERE fingerprint=?", (memory["fingerprint"],)).fetchone()
            hashes = sorted(set(hashes + (json.loads(old[0]) if old else [])))
            db.execute("INSERT OR REPLACE INTO memory_suppressions VALUES(?,?)", (memory["fingerprint"], encode(hashes)))
            self.invalidate_about(db)
            db.execute("DELETE FROM memories WHERE id=?", (identifier,))
            self.schedule_portrait(db)

    def portrait_context(self, db=None):
        if db is None:
            with self.connection() as db:
                return self.portrait_context(db)
        # Original details of corrected memories are deliberately not sent again.
        context = {"memories": [{"id": m["id"], "title": m["title"] if m["status"] != "corrected" else "用户已修正的记忆",
                                 "detail": m["correction"] if m["status"] == "corrected" else m["detail"],
                                 "category": m["category"], "horizon": m["horizon"], "period": m["period"],
                                 "status": m["status"], "sources": m["sources"]} for m in self.memories(db)]}
        if self.settings(db).get("growth_enabled"):
            # Fixed-size, deterministic mixture: corrections, recent changes and older context.
            values = context["memories"]
            chosen = [m for m in values if m["status"] != "pending"][:8]
            candidates = values[:12] + values[-8:] + values[::max(1, len(values) // 8)]
            for memory in candidates:
                if memory not in chosen and len(chosen) < 24:
                    chosen.append(memory)
            bounded = []
            for memory in chosen:
                item = {k: v for k, v in memory.items() if k != "sources"}
                if len(encode(bounded + [item])) <= 24000:
                    bounded.append(item)
            context["memories"] = bounded
            context["scope"] = {"included": len(bounded), "available": len(values), "note": "当前线索节选，不代表全部经历；不要声称完整了解用户"}
        return context, signature(context)

    def month_context(self, month, db=None):
        month_valid(month)
        if db is None:
            with self.connection() as db:
                return self.month_context(month, db)
        year, number = map(int, month.split("-"))
        previous = f"{year - (number == 1):04d}-{12 if number == 1 else number - 1:02d}"
        records = [r for r in self.about_records(db) if r["period"][:7] in (month, previous)]
        coverage = None
        if self.settings(db).get("growth_enabled"):
            selected = []
            receipts = {r["record_id"]: dict(r) for r in db.execute("SELECT * FROM growth_receipts")}
            for period in (month, previous):
                eligible = []
                for record in records:
                    receipt = receipts.get(record["id"])
                    if record["period"][:7] != period or not self.receipt_current(record, receipt):
                        continue
                    parts = json.loads(receipt["data"])["chunks"]
                    quotes = list(dict.fromkeys([e["quote"] for c in parts for e in c["events"]] +
                                      [s["quote"] for c in parts for m in c["memories"] for s in m["sources"]]))
                    if quotes:
                        eligible.append({**record, "body": "\n…\n".join(quotes[:3])[:1600]})
                eligible.sort(key=lambda r: (r["period"], r["id"]))
                if len(eligible) > 10:
                    eligible = [eligible[round(i * (len(eligible) - 1) / 9)] for i in range(10)]
                selected.extend(eligible)
            coverage = {"included": len(selected), "available": len(records), "note": "根据已积累的线索节选回顾，不代表完整月份；未出现不等于没有发生"}
            records = selected
        ids = {r["id"] for r in records}
        decisions = [{"detail": m["correction"] if m["status"] == "corrected" else m["detail"], "status": m["status"],
                      "sources": [s for s in m["sources"] if s["record_id"] in ids]} for m in self.memories(db) if m["user_modified"] and any(s["record_id"] in ids for s in m["sources"])]
        context = {"month": month, "previous_month": previous, "records": records, "user_decisions": decisions}
        if coverage:
            context["scope"] = coverage
        return context, signature(context)

    def request_about(self, kind, target="current"):
        if kind not in ("memory", "portrait", "month"):
            raise ValueError("请选择有效整理方式")
        if kind != "month" and target != "current":
            raise ValueError("个人档案目标无效")
        with self.connection() as db:
            if self.settings(db).get("growth_enabled") and kind == "memory":
                self.schedule_growth(db, retry_failed=True)
                self.schedule_portrait(db, immediate=True)
                return
            context, _ = self.month_context(target, db) if kind == "month" else self.portrait_context(db) if kind == "portrait" else self.memory_context(db)
            if kind == "portrait" and not context["memories"]:
                raise ValueError("先选择资料并整理长期记忆，再生成画像")
            if kind != "portrait" and not context["records"]:
                raise ValueError("还没有允许用于长期记忆的相关资料")
            if kind == "month" and not any(r["period"].startswith(target) for r in context["records"]):
                raise ValueError("这个月还没有允许用于长期记忆的资料")
            if len(encode(context)) > 120000:
                raise ValueError("所选资料较多，请先缩小长期记忆的资料范围")
            _, current = self.month_context(target, db) if kind == "month" else self.portrait_context(db) if kind == "portrait" else self.memory_context(db)
            cached = db.execute("SELECT signature FROM personal_reports WHERE kind=? AND target=?", (kind, target)).fetchone()
            if cached and cached[0] == current:
                return
            existing = db.execute("SELECT status FROM jobs WHERE kind=? AND target=?", (kind, target)).fetchone()
            if not existing or existing[0] not in ("queued", "running"):
                self.enqueue(db, kind, target)

    def about_state(self, month):
        from app.organization import automatic, portrait_readiness, portrait_presentation, original_memory_preview
        with self.connection() as db:
            context, _ = self.memory_context(db)
            memories = self.memories(db)
            settings = self.settings(db)
            reports = {}
            for kind, target in (("portrait", "current"), ("month", month_valid(month))):
                report = db.execute("SELECT * FROM personal_reports WHERE kind=? AND target=?", (kind, target)).fetchone()
                _, current = self.portrait_context(db) if kind == "portrait" else self.month_context(target, db)
                if report and report["signature"] == current:
                    reports[kind] = {**dict(report), "data": json.loads(report["data"])}
                    if kind == "portrait":
                        reports[kind]["data"] = portrait_presentation(reports[kind]["data"], memories, settings)
            entries = [dict(r) for r in db.execute("""SELECT e.id,e.journal_day,e.body,e.ai_enabled,
              EXISTS(SELECT 1 FROM memory_permissions p WHERE p.entry_id=e.id) AS memory_enabled
              FROM entries e ORDER BY e.journal_day DESC,e.created_at DESC LIMIT 200""")]
            for entry in entries:
                entry["preview"] = entry.pop("body")[:120]
            jobs = [dict(r) for r in db.execute("SELECT kind,target,status,error FROM jobs WHERE kind IN ('memory','portrait','month')")]
            if self.settings(db).get("growth_enabled"):
                effective = {r["id"].split(":", 1)[1] for r in context["records"] if r["type"] == "entry"}
                for entry in entries:
                    entry["memory_enabled"] = int(entry["id"] in effective)
            return {"organization": {"mode": settings["organization_mode"], **portrait_readiness(memories)},
                    "original_preview": original_memory_preview(memories) if automatic(settings) else [],
                    "growth": self.growth_status(db), "experiences": [dict(r) for r in db.execute("SELECT * FROM experiences ORDER BY created_at DESC,id")],
                    "memories": memories, "entries": entries, "reports": reports, "jobs": jobs,
                    "source_count": len(context["records"]), "entry_source_count": sum(r["type"] == "entry" for r in context["records"])}

    @staticmethod
    def normalize_bundle(data):
        from app.growth import GROWTH_TABLES
        if not isinstance(data, dict) or data.get("format") != "shiguang-diary" or data.get("schema_version") not in (1, 2, 3):
            raise ValueError("不是受支持的拾光日记备份（格式版本 1、2 或 3）")
        base = ("entries", "entry_versions", "analyses", "actions", "action_sources", "action_history", "daily_summaries")
        expected = set(base + (ABOUT_TABLES if data["schema_version"] >= 2 else ()) + (GROWTH_TABLES if data["schema_version"] == 3 else ()))
        if not isinstance(data.get("tables"), dict) or set(data["tables"]) != expected:
            raise ValueError("备份表结构不完整")
        return {**data, "schema_version": 3, "tables": {**{t: [] for t in ABOUT_TABLES + GROWTH_TABLES}, **data["tables"]}}

    def validate_about_import(self, tables):
        from app.about_ai import validate_portrait, validate_month
        from app.ai import AIError
        entries = {e["id"]: e for e in tables["entries"]}
        permitted = {p["entry_id"] for p in tables["memory_permissions"]}
        experiences = {e["id"]: e for e in tables["experiences"]}
        for experience in experiences.values():
            field(experience["title"], 160)
            field(experience["body"], 20000)
            field(experience["period"], 80, True)
            if experience["chapter"] not in ("成长", "学习", "工作", "关系", "生活", "转折") or experience["revision"] < 1 or experience["ai_enabled"] not in (0, 1):
                raise ValueError("备份经历字段无效")
            datetime.fromisoformat(experience["created_at"])
            datetime.fromisoformat(experience["updated_at"])
        if any(not entries[i]["ai_enabled"] for i in permitted):
            raise ValueError("备份长期记忆授权与日记设置不一致")
        for source in tables["memory_sources"]:
            kind = "entry" if source["entry_id"] else "experience"
            row = entries[source["entry_id"]] if kind == "entry" else experiences[source["experience_id"]]
            if not row["ai_enabled"] or (kind == "entry" and row["id"] not in permitted) or source["revision"] != row["revision"] or not isinstance(source["quote"], str) or not source["quote"] or source["quote"] not in row["body"]:
                raise ValueError("备份记忆来源无效或未获授权")
        for memory in tables["memories"]:
            field(memory["title"], 160)
            field(memory["detail"], 2000)
            field(memory["period"], 80, True)
            field(memory["correction"], 3000, memory["status"] != "corrected")
            if not re.fullmatch(r"[0-9a-f]{64}", memory["fingerprint"]):
                raise ValueError("备份记忆标识无效")
            from app.growth import growth_fingerprint
            refs = [s for s in tables["memory_sources"] if s["memory_id"] == memory["id"]]
            valid_keys = {fingerprint(memory["category"], memory["title"])}
            if len(refs) == 1:
                record_id = "entry:" + refs[0]["entry_id"] if refs[0]["entry_id"] else "experience:" + refs[0]["experience_id"]
                valid_keys.add(growth_fingerprint(memory["category"], memory["title"], record_id))
            if memory["fingerprint"] not in valid_keys:
                raise ValueError("备份记忆标识与内容不一致")
            if memory["horizon"] == "recent" and not memory["period"].strip():
                raise ValueError("备份近期记忆缺少时间范围")
            if memory["category"] == "pattern" and sum(s["memory_id"] == memory["id"] for s in tables["memory_sources"]) < 2:
                raise ValueError("备份行为模式缺少多个来源")
            datetime.fromisoformat(memory["created_at"])
            datetime.fromisoformat(memory["updated_at"])
        for block in tables["memory_suppressions"]:
            hashes = json.loads(block["source_hashes"])
            if not re.fullmatch(r"[0-9a-f]{64}", block["fingerprint"]) or not isinstance(hashes, list) or len(hashes) > 10000 or any(not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes):
                raise ValueError("备份遗忘记录无效")
        # Reports are derived caches and are invalidated on import. Still reject
        # malformed data rather than silently accepting broken backups.
        for report in tables["personal_reports"]:
            value = json.loads(report["data"])
            if report["kind"] not in ("portrait", "month") or not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("items"), list):
                raise ValueError("备份个人回顾格式无效")
            if report["kind"] == "month":
                month_valid(report["target"])
            try:
                if report["kind"] == "portrait":
                    supported = {s["memory_id"] for s in tables["memory_sources"]}
                    memories = [{**m, "detail": m["correction"] if m["status"] == "corrected" else m["detail"]}
                                for m in tables["memories"] if m["id"] in supported]
                    validate_portrait(value, {"memories": memories})
                else:
                    month = report["target"]
                    year, number = map(int, month.split("-"))
                    previous = f"{year - (number == 1):04d}-{12 if number == 1 else number - 1:02d}"
                    records = [{"id": "entry:" + e["id"], "body": e["body"], "period": e["journal_day"], "title": e["journal_day"]}
                               for e in entries.values() if e["ai_enabled"] and e["id"] in permitted and e["journal_day"][:7] in (month, previous)]
                    records += [{"id": "experience:" + e["id"], "body": e["body"], "period": e["period"], "title": e["title"]}
                                for e in experiences.values() if e["ai_enabled"] and e["period"][:7] in (month, previous)]
                    validate_month(value, {"records": records, "month": month, "previous_month": previous})
            except AIError as exc:
                raise ValueError("备份个人回顾缺少有效依据") from exc
