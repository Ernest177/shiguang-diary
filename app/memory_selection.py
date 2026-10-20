"""Bounded, evidence-aware portrait context. Selection never deletes memories."""
from datetime import date
import json
import re

SELECTION_VERSION = "memory-selection.v1"
MAX_MEMORIES = 24
MAX_CHARACTERS = 24000
CATEGORIES = ("fact", "value", "preference", "goal", "pattern")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def period_key(value):
    """Use only explicit calendar periods; never turn ingestion time into life time."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value):
        return ""
    try:
        date.fromisoformat(value + {4: "-01-01", 7: "-01", 10: ""}[len(value)])
    except ValueError:
        return ""
    return value


def select_memories(memories, max_items=MAX_MEMORIES, max_characters=MAX_CHARACTERS):
    """Reserve room for decisions, recent life, long-term context and early history.

    Remaining space balances categories, periods and independent sources. The
    score is a local coverage heuristic, not a judgement of a person's importance.
    Full details are kept; an item that does not fit is skipped, never cut midway.
    """
    values = sorted(memories, key=lambda m: m["id"])
    by_id = {m["id"]: m for m in values}
    periods = {m["id"]: sorted({p for s in m["sources"] if (p := period_key(s.get("period")))}) for m in values}
    sources = {m["id"]: {s["record_id"] for s in m["sources"]} for m in values}
    items = {}
    for memory in values:
        corrected = memory["status"] == "corrected"
        items[memory["id"]] = {
            "id": memory["id"], "title": "用户已修正的记忆" if corrected else memory["title"],
            "detail": memory["correction"] if corrected else memory["detail"],
            **{key: memory[key] for key in ("category", "horizon", "period", "status")},
        }
    chosen, seen, covered_sources, covered_periods, covered_categories = [], set(), set(), set(), set()

    def priority(memory):
        identifier = memory["id"]
        return (memory["status"] == "corrected", memory["status"] == "confirmed",
                len(sources[identifier] - covered_sources),
                bool(set(periods[identifier]) - covered_periods),
                memory["category"] not in covered_categories,
                max(periods[identifier], default=""), identifier)

    def take(candidates):
        for memory in candidates:
            identifier = memory["id"]
            if identifier in seen or len(chosen) >= max_items:
                continue
            item = items[identifier]
            if len(encoded(chosen + [item])) > max_characters:
                continue
            chosen.append(item)
            seen.add(identifier)
            covered_sources.update(sources[identifier])
            covered_periods.update(periods[identifier])
            covered_categories.add(memory["category"])
            return True
        return False

    # These anchors prevent a burst of new/backfilled records from crowding out
    # earlier experiences, and prevent confirmations from consuming the whole budget.
    take(sorted((m for m in values if m["status"] != "pending"), key=priority, reverse=True))
    dated = [m for m in values if periods[m["id"]]]
    take(sorted(dated, key=lambda m: (max(periods[m["id"]]), m["id"]), reverse=True))
    take(sorted((m for m in values if m["horizon"] == "long_term"), key=priority, reverse=True))
    take(sorted(dated, key=lambda m: (min(periods[m["id"]]), m["id"])))
    for category in CATEGORIES:
        take(sorted((m for m in values if m["category"] == category), key=priority, reverse=True))
    while len(chosen) < max_items and take(sorted(values, key=priority, reverse=True)):
        pass
    scope = {
        "selection_version": SELECTION_VERSION, "included": len(chosen), "available": len(values),
        "max_items": max_items, "max_characters": max_characters, "selected_characters": len(encoded(chosen)),
        "decisions": sum(by_id[m["id"]]["status"] != "pending" for m in chosen),
        "long_term": sum(m["horizon"] == "long_term" for m in chosen),
        "categories": sorted(covered_categories), "source_count": len(covered_sources),
        "period_start": min(covered_periods, default=""), "period_end": max(covered_periods, default=""),
        "note": "兼顾用户修正、近期记录、长期线索和早期经历的节选；未选入不等于没有价值，也不代表完整了解用户",
    }
    return chosen, scope
