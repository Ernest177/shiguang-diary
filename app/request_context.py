"""Small wire contexts and source-bound local receipts; no cross-user result cache."""
from copy import deepcopy

from app.about import signature
from app.prompts import prompt_metadata

CONTEXT_VERSION = "context.v1"
REF_FIELDS = ("entry_id", "record_id", "action_id", "existing_action_id", "growth_record_id")


def prepare_request(kind, context):
    """Alias structured identifiers only. Never rewrite body, quotes or other text."""
    payload = deepcopy(context)
    maps = {k: {} for k in REF_FIELDS}
    memory_map = {}
    if kind == "entry":
        original = context["entry"]
        payload["entry"] = {k: original[k] for k in ("id", "body", "journal_day", "created_at", "timezone") if k in original}
        payload["entry"]["id"] = "r1"
        maps["entry_id"][original["id"]] = "r1"
        if context.get("growth_record_id"):
            maps["record_id"][context["growth_record_id"]] = "r1"
            maps["growth_record_id"][context["growth_record_id"]] = "r1"
    for index, record in enumerate(payload.get("records", []), 1):
        original_id, alias = record["id"], "r" + str(index)
        maps["entry_id" if kind == "day" else "record_id"][original_id] = alias
        record["id"] = alias
        record.pop("revision", None)
        if kind == "day":
            record.pop("analysis", None)  # The full original already supplies the evidence.
    for collection in ("existing_actions", "actions"):
        for index, action in enumerate(payload.get(collection, []), 1):
            original_id, alias = action["id"], "a" + str(index)
            maps["action_id"][original_id] = alias
            maps["existing_action_id"][original_id] = alias
            action["id"] = alias
            if kind == "day":
                action["sources"] = [{"entry_id": s["entry_id"]} for s in action.get("sources", []) if s["entry_id"] in maps["entry_id"]]
    for index, memory in enumerate(payload.get("memories", []), 1):
        memory_map[memory["id"]] = "m" + str(index)
        memory["id"] = memory_map[memory["id"]]
        memory.pop("sources", None)  # Portraits cite memory IDs; raw quotes stay local.

    def translate(value, mappings, memories):
        if isinstance(value, list):
            return [translate(v, mappings, memories) for v in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key in mappings and isinstance(item, str):
                result[key] = mappings[key].get(item, item)
            elif key == "memory_ids" and isinstance(item, list):
                result[key] = [memories.get(i, i) if isinstance(i, str) else i for i in item]
            else:
                result[key] = translate(item, mappings, memories)
        return result

    payload = translate(payload, maps, memory_map)
    reverse = {key: {alias: original for original, alias in items.items()} for key, items in maps.items()}
    reverse_memories = {alias: original for original, alias in memory_map.items()}
    return payload, lambda data: translate(data, reverse, reverse_memories)


def receipt_key(kind, context, settings):
    payload, _ = prepare_request(kind, context)
    # Include original identities: r1 in two requests must never share a result.
    return signature({"context_version": CONTEXT_VERSION, "kind": kind, "context": context,
                      "payload": payload, "prompt": prompt_metadata()[kind], "mode": settings["ai_mode"],
                      "service": settings["ai_base_url"].rstrip("/"), "model": settings["ai_model"]})


def cached_result(data, key):
    if not isinstance(data, dict):
        return False
    meta = data.get("_cache")
    value = {k: v for k, v in data.items() if k != "_cache"}
    return isinstance(meta, dict) and meta.get("version") == 1 and meta.get("key") == key and meta.get("result") == signature(value)


def with_receipt(data, key):
    value = {k: v for k, v in data.items() if k != "_cache"}
    return {**value, "_cache": {"version": 1, "key": key, "result": signature(value)}}
