"""Transparent evidence-anchored scoring; this is not a semantic truth oracle."""
from __future__ import annotations

from collections import Counter
import re

from app.ai import AIError, validate_entry, validate_day

SCORER_VERSION = "evidence.v1"


def items(value):
    return value if isinstance(value, list) else []


def actions_score(actual, expected, body):
    actual = items(actual)
    used = set()
    tp = dates = links = matched = critical = unsupported = 0
    for action in actual:
        if not isinstance(action, dict):
            unsupported += 1
            critical += 1
            continue
        quote = action.get("quote")
        supported = isinstance(quote, str) and bool(quote.strip()) and quote in body
        unsupported += not supported
        match = None
        for index, label in enumerate(expected):
            if index in used or action.get("kind") != label["kind"] or not supported:
                continue
            if label["anchor"] in quote and re.search(label["title_pattern"], str(action.get("title", ""))):
                match = index
                break
        if match is None:
            critical += action.get("kind") == "task"
        else:
            used.add(match)
            matched += 1
            label = expected[match]
            dates += action.get("due_date") == label.get("due_date")
            links += action.get("existing_action_id") == label.get("existing_action_id")
            tp += 1
    return {"tp": tp, "fp": len(actual) - tp, "fn": len(expected) - tp,
            "matched": matched, "correct_dates": dates, "correct_links": links,
            "critical_false_tasks": critical, "unsupported_actions": unsupported,
            "action_count": len(actual)}


def completions_score(actual, expected, body):
    remaining = Counter(expected)
    correct = unexpected = 0
    for item in items(actual):
        if not isinstance(item, dict):
            unexpected += 1
            continue
        identifier, quote = item.get("action_id"), item.get("quote")
        supported = isinstance(quote, str) and bool(quote.strip()) and quote in body
        if supported and isinstance(identifier, str) and remaining[identifier] > 0:
            correct += 1
            remaining[identifier] -= 1
        else:
            unexpected += 1
    return {"correct_completions": correct, "unexpected_completions": unexpected,
            "expected_completions": len(expected)}


def quote_counts(output, case):
    valid = count = 0
    if not isinstance(output, dict):
        return {"quote_count": 0, "valid_quotes": 0}
    if case["kind"] == "entry":
        body = case["input"]["entry"]["body"]
        for field in ("insights", "actions", "completions"):
            for item in items(output.get(field)):
                count += 1
                quote = item.get("quote") if isinstance(item, dict) else None
                valid += isinstance(quote, str) and bool(quote.strip()) and quote in body
    else:
        records = {r["id"]: r["body"] for r in case["input"]["records"]}
        for section in items(output.get("sections")):
            for item in items(section.get("items")) if isinstance(section, dict) else []:
                for source in items(item.get("sources")) if isinstance(item, dict) else []:
                    count += 1
                    quote = source.get("quote") if isinstance(source, dict) else None
                    identifier = source.get("entry_id") if isinstance(source, dict) else None
                    valid += isinstance(quote, str) and bool(quote.strip()) and isinstance(identifier, str) and quote in records.get(identifier, "")
    return {"quote_count": count, "valid_quotes": valid}


def score_case(case, output):
    """Keep raw model mistakes separate from application filtering/rejection."""
    raw = output if isinstance(output, dict) else {}
    error = None
    try:
        if case["kind"] == "entry":
            final = validate_entry(output, case["input"]["entry"], case["input"]["existing_actions"])
        else:
            final = validate_day(output, case["input"])
    except (AIError, ValueError, TypeError, AttributeError, KeyError) as exc:
        final, error = {}, type(exc).__name__
    result = {"schema_valid": int(error is None), "validation_error": error,
              "raw_quotes": quote_counts(raw, case), "final_quotes": quote_counts(final, case)}
    if case["kind"] == "entry":
        expected = case["expected"]
        body = case["input"]["entry"]["body"]
        for key, value in (("raw", raw), ("final", final)):
            result[key] = {**actions_score(value.get("actions"), expected["actions"], body),
                           **completions_score(value.get("completions"), expected["completions"], body)}
    else:
        required = set(case["expected"]["source_ids"])
        found = set()
        records = {r["id"]: r["body"] for r in case["input"]["records"]}
        for section in items(final.get("sections")):
            for item in items(section.get("items")):
                for source in items(item.get("sources")):
                    if source["quote"] in records[source["entry_id"]]:
                        found.add(source["entry_id"])
        result["day"] = {"required_sources": len(required), "covered_sources": len(required & found)}
    return result


def ratio(numerator, denominator):
    return round(numerator / denominator, 6) if denominator else None


def aggregate(samples):
    metrics = {"cases": len(samples), "entry_cases": 0, "day_cases": 0,
               "schema_valid": 0, "request_errors": 0}
    counts = {"raw": Counter(), "final": Counter()}
    day, quotes = Counter(), Counter()
    for sample in samples:
        score = sample["score"]
        metrics["schema_valid"] += score["schema_valid"]
        metrics["request_errors"] += sample.get("error") is not None
        metrics[sample["kind"] + "_cases"] += 1
        if sample["kind"] == "entry":
            for layer in counts:
                counts[layer].update(score[layer])
        else:
            day.update(score["day"])
        quotes.update(score["raw_quotes"])
    metrics["schema_valid_rate"] = ratio(metrics["schema_valid"], len(samples))
    metrics["raw_quote_valid_rate"] = ratio(quotes["valid_quotes"], quotes["quote_count"])
    metrics["day_source_coverage"] = ratio(day["covered_sources"], day["required_sources"])
    for layer, count in counts.items():
        metrics[layer] = dict(count)
        metrics[layer].update({"precision": ratio(count["tp"], count["tp"] + count["fp"]),
                              "recall": ratio(count["tp"], count["tp"] + count["fn"]),
                              "f1": ratio(2 * count["tp"], 2 * count["tp"] + count["fp"] + count["fn"]),
                              "date_accuracy": ratio(count["correct_dates"], count["matched"]),
                              "link_accuracy": ratio(count["correct_links"], count["matched"]),
                              "completion_recall": ratio(count["correct_completions"], count["expected_completions"])})
    return metrics


def gate(metrics):
    """Small-corpus regression gate, not a claim of production accuracy."""
    reasons = []
    if not metrics["cases"] or metrics["request_errors"]:
        reasons.append("请求失败或没有样例")
    if metrics["schema_valid_rate"] != 1:
        reasons.append("存在结构或引用校验失败")
    if metrics["raw_quote_valid_rate"] not in (None, 1):
        reasons.append("原始输出存在无效引用")
    for layer in ("raw", "final"):
        score = metrics[layer]
        if score.get("critical_false_tasks", 0) or score.get("unexpected_completions", 0):
            reasons.append(layer + " 存在误提待办或误关闭")
        for key in ("f1", "date_accuracy", "link_accuracy", "completion_recall"):
            if score[key] is not None and score[key] < 0.9:
                reasons.append(layer + " " + key + " 低于 0.9")
    if metrics["day_source_coverage"] is not None and metrics["day_source_coverage"] < 0.9:
        reasons.append("每日回顾来源覆盖低于 0.9")
    return {"passed": not reasons, "reasons": reasons}
