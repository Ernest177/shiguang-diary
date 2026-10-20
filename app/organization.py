"""Conservative local presentation choices, separate from permission to process AI data."""
from copy import deepcopy
from difflib import SequenceMatcher
import re
import unicodedata

ORGANIZATION_VERSION = "organization.v1"
ORGANIZATION_DEFAULTS = {"organization_mode": "auto", "original_similarity": 88, "brief_entry_chars": 500}


def normalized(value):
    value = unicodedata.normalize("NFKC", str(value)).lower()
    return "".join(c for c in value if c.isalnum())


def near_original(value, original, threshold=88):
    """A display heuristic, never a semantic equivalence or fact-correctness test."""
    left, right = normalized(value), normalized(original)
    if not left or not right or min(len(left), len(right)) < 8:
        return left == right and bool(left)
    # A substantially shorter summary can be useful even with shared wording.
    if len(left) < len(right) * .8 or max(len(left), len(right)) > 3000:
        return False
    return SequenceMatcher(None, left, right, autojunk=False).ratio() >= threshold / 100


def automatic(settings):
    return settings.get("organization_mode", "auto") == "auto"


def brief_original(context, settings):
    if not automatic(settings) or len(context["records"]) != 1:
        return False
    body = context["records"][0]["body"].strip()
    if not body or len(body) > settings.get("brief_entry_chars", 500):
        return False
    # Only explicit requests about expression trigger this guard. Planning to
    # "理清项目思路" is an action, not evidence that the diary needs rewriting.
    if re.search(r"表达不清|说不清|写得[很太]?乱|思路[很太]?混乱|不知道[该要]?怎么表达|不通顺|帮我(?:整理|梳理)(?:一下)?这(?:段|篇)", body):
        return False
    # Punctuation density is not complexity: a few short reminders may contain
    # many full stops and still gain nothing from an additional paraphrase.
    return len([p for p in body.splitlines() if p.strip()]) <= 5


def original_day_data():
    return {"schema_version": 1, "overview": "保留今天的原文。", "sections": [],
            "_organization": {"version": ORGANIZATION_VERSION, "kind": "original"}}


def day_presentation(data, context, settings):
    if not data or not automatic(settings) or data.get("_organization", {}).get("forced"):
        return data
    output = deepcopy(data)
    if brief_original(context, settings):
        output["presentation"] = {"kind": "original", "records": [
            {"entry_id": r["id"], "body": r["body"]} for r in context["records"]],
            "local": output.get("_organization", {}).get("kind") == "original"}
        return output
    threshold = settings.get("original_similarity", 88)
    replaced = 0
    for section in output["sections"]:
        if section["category"] == "actions":
            continue  # Never replace action status/term interpretation by text similarity.
        for item in section["items"]:
            quotes = list(dict.fromkeys(s["quote"] for s in item["sources"]))
            if len(quotes) == 1 and near_original(item["text"], quotes[0], threshold):
                item["text"], item["presentation"] = quotes[0], "original"
                replaced += 1
    if replaced:
        output["presentation"] = {"kind": "mixed", "reused": replaced}
    return output


def portrait_readiness(memories):
    ids, periods = set(), set()
    for memory in memories:
        for source in memory["sources"]:
            ids.add(source["record_id"])
            if source.get("period", "").strip():
                periods.add(source["period"].strip())
    return {"ready": len(ids) >= 3 and len(periods) >= 2, "records": len(ids), "periods": len(periods),
            "minimum_records": 3, "minimum_periods": 2}


def portrait_presentation(data, memories, settings):
    if not automatic(settings):
        return data
    result = deepcopy(data)
    if not portrait_readiness(memories)["ready"]:
        result["items"] = []
        return result
    known = {m["id"]: m for m in memories}
    items = []
    for item in data["items"]:
        related = [known[i] for i in item["memory_ids"] if i in known]
        records = {s["record_id"] for m in related for s in m["sources"]}
        # Compare the derived portrait only with the user's own source wording.
        # Another AI-generated memory detail is not an original diary passage.
        originals = [m["correction"] for m in related if m["status"] == "corrected"]
        originals += [s["quote"] for m in related if m["status"] != "corrected" for s in m["sources"]]
        if len(records) >= 2 and not any(near_original(item["text"], text, settings.get("original_similarity", 88)) for text in originals):
            items.append(item)
    result["items"] = items
    return result


def original_memory_preview(memories):
    output, seen = [], set()
    for memory in memories:
        for source in memory["sources"]:
            text = memory["correction"] if memory["status"] == "corrected" else source["quote"]
            key = (source["record_id"], text)
            # Collapse only an identical citation from the same original record.
            # Similar wording on another date remains a distinct occurrence.
            if key not in seen:
                seen.add(key)
                output.append({"text": text, "label": "你的修正" if memory["status"] == "corrected" else "原文", "source": source})
            if len(output) == 3:
                return output
    return output
