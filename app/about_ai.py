"""Personal memory validation and jobs. AI interpretations remain user-reviewable."""
import json
import re
import uuid

from app.about import encode, fingerprint, source_hash, stamp
from app.ai import AIError, array, evidence, text
from app.prompts import MEMORY_PROMPT, PORTRAIT_PROMPT, MONTH_PROMPT, prompt_metadata


def sources(value, records):
    output, seen = [], set()
    for source in array(value, "sources", 6):
        identifier = source.get("record_id")
        if not isinstance(identifier, str) or identifier not in records or identifier in seen:
            raise AIError("长期记忆来源无效或重复")
        quote = evidence(source.get("quote"), records[identifier]["body"])
        if len(quote) > 1200:
            raise AIError("长期记忆引用过长，请缩短引用")
        output.append({"record_id": identifier, "quote": quote})
        seen.add(identifier)
    if not output:
        raise AIError("长期记忆缺少原文依据")
    return output


def validate_memories(data, context):
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise AIError("长期记忆格式无效")
    records = {r["id"]: r for r in context["records"]}
    output, seen = [], set()
    for item in array(data, "memories", 24):
        category, horizon = item.get("category"), item.get("horizon")
        if category not in ("fact", "value", "preference", "goal", "pattern") or horizon not in ("long_term", "recent", "unknown"):
            raise AIError("长期记忆类别或时间范围无效")
        title = text(item.get("title"), 160)
        detail = text(item.get("detail"), 2000)
        period = item.get("period", "")
        if not isinstance(period, str) or len(period) > 80 or (horizon == "recent" and not period.strip()):
            raise AIError("近期记忆需要保留时间范围")
        refs = sources(item, records)
        if horizon == "long_term" and any(re.search(r"最近|近[一二三四五六七八九十几两0-9]+(?:天|周|个月|月)|这(?:一)?(?:周|月|段时间)|这几天|本(?:周|月)|这阵子", s["quote"]) for s in refs):
            horizon = "recent"
            if not period.strip():
                period = " / ".join(dict.fromkeys(records[s["record_id"]]["period"] for s in refs)) or "原文所述近期"
        if category == "pattern" and len(refs) < 2:
            raise AIError("行为模式需要至少两条不同记录，请保留为具体经历")
        key = fingerprint(category, title)
        if key in seen:
            raise AIError("长期记忆重复")
        seen.add(key)
        output.append({"title": title, "detail": detail, "category": category, "horizon": horizon,
                       "period": period.strip(), "sources": refs})
    return {"schema_version": 1, "memories": output}


def validate_portrait(data, context):
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise AIError("个人画像格式无效")
    known = {m["id"]: m for m in context["memories"]}
    output = []
    for item in array(data, "items", 12):
        if item.get("category") not in ("focus", "values", "preferences", "strengths", "questions") or item.get("certainty") not in ("observation", "hypothesis"):
            raise AIError("个人画像类别无效")
        ids = array(item, "memory_ids", 6)
        if not ids or any(not isinstance(i, str) or i not in known for i in ids) or len(ids) != len(set(ids)):
            raise AIError("个人画像缺少有效记忆依据")
        value = text(item.get("text"), 1000)
        # A new interpretation cannot promote itself to a confirmed personal fact.
        exact_confirmed = len(ids) == 1 and known[ids[0]]["status"] in ("confirmed", "corrected") and value == known[ids[0]]["detail"]
        related = [known[i] for i in ids]
        periods = list(dict.fromkeys(m["period"] for m in related if m["period"]))
        output.append({"text": value, "category": item["category"], "memory_ids": ids,
                       "certainty": "observation" if exact_confirmed else "hypothesis",
                       "horizon": "recent" if any(m["horizon"] == "recent" for m in related) else "unknown",
                       "period": " / ".join(periods)})
    return {"schema_version": 1, "items": output}


def validate_month(data, context):
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise AIError("月度回顾格式无效")
    records = {r["id"]: r for r in context["records"]}
    output = []
    for item in array(data, "items", 12):
        if item.get("category") not in ("events", "growth", "questions") or not isinstance(item.get("comparison"), bool):
            raise AIError("月度回顾类别无效")
        refs = sources(item, records)
        periods = {records[s["record_id"]]["period"][:7] for s in refs}
        if context["month"] not in periods:
            raise AIError("月度回顾缺少当月依据")
        if item["comparison"] and context["previous_month"] not in periods:
            raise AIError("变化对比必须同时引用本月与上月的记录")
        output.append({"text": text(item.get("text"), 1000), "category": item["category"],
                       "comparison": item["comparison"], "sources": [{**s, "title": records[s["record_id"]]["title"],
                       "period": records[s["record_id"]]["period"]} for s in refs]})
    return {"schema_version": 1, "items": output}


def execute_about(worker, job, settings):
    store, kind, target = worker.store, job["kind"], job["target"]
    context_fn = (lambda db=None: store.month_context(target, db)) if kind == "month" else store.portrait_context if kind == "portrait" else store.memory_context
    context, before = context_fn()
    if not (context.get("memories") if kind == "portrait" else context.get("records")):
        return
    if len(encode(context)) > 120000:
        raise AIError("所选资料较多，请缩小长期记忆的资料范围")
    prompt = {"memory": MEMORY_PROMPT, "portrait": PORTRAIT_PROMPT, "month": MONTH_PROMPT}[kind]
    validator = {"memory": validate_memories, "portrait": validate_portrait, "month": validate_month}[kind]
    payload = {k: v for k, v in context.items() if k != "suppressed"}
    result = validator(worker.client.call(settings, prompt, payload), context)
    with store.connection() as db:
        _, after = context_fn(db)
        if before != after or not db.execute("SELECT 1 FROM jobs WHERE id=?", (job["id"],)).fetchone():
            return
        if kind == "memory":
            records = {r["id"]: r for r in context["records"]}
            protected = [m for m in store.memories(db) if m["user_modified"]]
            protected_hashes = {source_hash(m["category"], s) for m in protected for s in m["sources"]}
            blocks = [dict(r) for r in db.execute("SELECT * FROM memory_suppressions")]
            suppressed = {r["fingerprint"] for r in blocks}
            blocked_sources = {h for r in blocks for h in json.loads(r["source_hashes"])}
            db.execute("DELETE FROM memories WHERE user_modified=0")
            for memory in result["memories"]:
                key = fingerprint(memory["category"], memory["title"])
                hashes = {source_hash(memory["category"], s) for s in memory["sources"]}
                if key in suppressed or hashes & (blocked_sources | protected_hashes):
                    continue
                if db.execute("SELECT 1 FROM memories WHERE fingerprint=?", (key,)).fetchone():
                    continue
                identifier, updated = str(uuid.uuid4()), stamp()
                db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (identifier, key, memory["title"], memory["detail"],
                           memory["category"], memory["horizon"], memory["period"], "pending", "", 0, updated, updated))
                for source in memory["sources"]:
                    record = records[source["record_id"]]
                    source_type, source_id = source["record_id"].split(":", 1)
                    db.execute("INSERT INTO memory_sources VALUES(?,?,?,?,?)", (identifier, source_id if source_type == "entry" else None,
                               source_id if source_type == "experience" else None, record["revision"], source["quote"]))
            db.execute("DELETE FROM personal_reports")
            if store.memories(db):
                store.enqueue(db, "portrait", "current")
        else:
            info = prompt_metadata()[kind]
            db.execute("INSERT OR REPLACE INTO personal_reports VALUES(?,?,?,?,?,?,?,?)", (kind, target, before, encode(result),
                       settings["ai_model"], info["version"], info["sha256"], stamp()))
