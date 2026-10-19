"""Reproducible synthetic input-size comparison. No models, keys or diary databases."""
import json
from pathlib import Path
import sys
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.request_context import CONTEXT_VERSION, prepare_request
from app.store import compact


def compare():
    records, actions = [], []
    for number in range(6):
        identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, "synthetic-cost-entry-" + str(number)))
        action_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "synthetic-cost-action-" + str(number)))
        body = f"【合成片段{number + 1}】今天整理了学习笔记，发现用自己的话解释更容易发现理解的缺口。明天联系同伴讨论这些问题。"
        action = {"title": "联系同伴讨论问题", "kind": "task", "quote": body[-15:], "due_text": "明天", "due_date": "2026-10-21", "existing_action_id": None}
        records.append({"id": identifier, "body": body, "revision": 1, "created_at": "2026-10-20T08:00:00+00:00", "timezone": "Asia/Shanghai",
                        "analysis": {"schema_version": 1, "summary": body, "insights": [{"text": "尝试复述笔记", "quote": body[:45], "type": "idea"}], "actions": [action], "completions": []}})
        actions.append({"id": action_id, "title": action["title"], "kind": "task", "status": "open", "due_text": "明天", "due_date": "2026-10-21",
                        "sources": [{"entry_id": identifier, "revision": 1, "quote": action["quote"]}]})
    original = {"day": "2026-10-20", "records": records, "actions": actions}
    payload, _ = prepare_request("day", original)
    before, after = len(compact(original)), len(compact(payload))
    assert after < before, "The compact synthetic request must remain smaller than its full context"
    assert [r["body"] for r in original["records"]] == [r["body"] for r in payload["records"]]
    return {"context_version": CONTEXT_VERSION, "synthetic_records": len(records), "before_characters": before,
            "after_characters": after, "character_reduction_percent": round((before - after) * 100 / before, 1),
            "original_bodies_unchanged": True, "notice": "Serialized JSON characters only; not measured tokens, billing or model quality."}


if __name__ == "__main__":
    print(json.dumps(compare(), ensure_ascii=False, indent=2))
