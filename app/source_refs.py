"""Lossless source segments: models select IDs instead of retyping noisy quotations."""
from copy import deepcopy
import re

SOURCE_REFS_VERSION = "source-refs.v1"


def segments(body, maximum=240):
    pieces, position = [], 0
    for match in re.finditer(r"[。！？!?；;\n]+|$", body):
        end = match.end()
        while end - position > maximum:
            pieces.append(body[position:position + maximum]); position += maximum
        if end > position:
            pieces.append(body[position:end]); position = end
    if position < len(body): pieces.append(body[position:])
    assert "".join(pieces) == body
    return pieces


def entry_references(content):
    """Prepare one entry; source IDs remain bound to this request and this record."""
    return references(content, "entry")


def day_references(content):
    return references(content, "day")


def references(content, kind):
    value = deepcopy(content)
    records = [value.get("entry", {})] if kind == "entry" else value.get("records", [])
    if not isinstance(records, list) or not records or any(not isinstance(r, dict) or not isinstance(r.get("body"), str) or not isinstance(r.get("id"), str) for r in records):
        return value, lambda output: output
    known = {}
    for record in records:
        parts = []
        for part in segments(record.pop("body")):
            key = "q" + str(len(known) + 1)
            known[key] = (record["id"], part)
            parts.append({"id": key, "text": part})
        record["segments"] = parts
    value["source_reference_format"] = SOURCE_REFS_VERSION

    def decode(node):
        if isinstance(node, list): return [decode(item) for item in node]
        if not isinstance(node, dict): return node
        result = {key: decode(item) for key, item in node.items() if key != "quote_id"}
        if "quote_id" in node:
            identifier = node["quote_id"]
            if not isinstance(identifier, str) or identifier not in known:
                raise ValueError("AI 引用了不存在的原文片段，已拒绝本次结果")
            owner, quote = known[identifier]
            if kind == "day" and "entry_id" not in node:
                raise ValueError("AI 引用缺少来源记录编号")
            for field in ("entry_id", "record_id"):
                if field in node and node[field] != owner:
                    raise ValueError("AI 引用的片段与来源记录不匹配")
            result["quote"] = quote.strip()
            if not result["quote"]: raise ValueError("AI 引用了空白原文片段")
        return result

    return value, decode
