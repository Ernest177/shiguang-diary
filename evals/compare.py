"""Refuse mismatched evaluation inputs; never compare fixture scores to live scores."""
import argparse
import json
from pathlib import Path


def compare(baseline, candidate):
    reasons = []
    for key in ("report_version", "mode", "corpus", "scorer_version", "gate_version", "selected_ids"):
        if baseline.get(key) != candidate.get(key):
            reasons.append("不可直接比较：" + key + " 不同")
    if baseline.get("mode") == "contract" and baseline.get("fixture_sha256") != candidate.get("fixture_sha256"):
        reasons.append("不可直接比较：手写替身改变，需要人工审查并建立新基线")
    for kind, before in baseline.get("prompts", {}).items():
        after = candidate.get("prompts", {}).get(kind, {})
        if before.get("version") == after.get("version") and before.get("sha256") != after.get("sha256"):
            reasons.append("不可直接比较：提示词复用旧版本覆盖内容 " + kind)
    if reasons:
        return {"passed": False, "comparable": False, "reasons": reasons}
    if not candidate["gate"]["passed"]:
        reasons.extend(candidate["gate"]["reasons"])
    old, new = baseline["metrics"], candidate["metrics"]
    # Require no observed regression on this small fixed suite; large live suites can
    # later introduce confidence intervals and a separately versioned gate policy.
    for layer in ("raw", "final"):
        for key in ("f1", "recall", "precision", "date_accuracy", "link_accuracy", "completion_recall"):
            before, after = old[layer][key], new[layer][key]
            if before is not None and (after is None or after < before - .000001):
                reasons.append(layer + " " + key + " 退步")
    for key in ("schema_valid_rate", "raw_quote_valid_rate", "day_source_coverage"):
        before, after = old[key], new[key]
        if before is not None and (after is None or after < before - .000001):
            reasons.append(key + " 退步")
    old_samples = {s["id"]: s for s in baseline.get("samples", [])}
    for sample in candidate.get("samples", []):
        previous = old_samples.get(sample["id"])
        if not previous:
            continue
        for layer in ("raw", "final"):
            before, after = previous["score"].get(layer, {}), sample["score"].get(layer, {})
            for key in ("fp", "fn", "critical_false_tasks", "unexpected_completions"):
                if after.get(key, 0) > before.get(key, 0):
                    reasons.append(sample["id"] + " " + layer + " " + key + " 退步")
            for key in ("correct_dates", "correct_links", "correct_completions"):
                if after.get(key, 0) < before.get(key, 0):
                    reasons.append(sample["id"] + " " + layer + " " + key + " 退步")
    return {"passed": not reasons, "comparable": True, "reasons": reasons}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    result = compare(json.loads(args.baseline.read_text(encoding="utf-8")),
                     json.loads(args.candidate.read_text(encoding="utf-8")))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
