"""Check personal-archive contracts offline; never score these as model accuracy."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import uuid

from app import __version__
from app.ai import AIError
from app.about_ai import validate_memories, validate_portrait, validate_month
from evals.run import DATA, ROOT, check_prompts, digest, read_json, revision


def evaluate():
    corpus = read_json(DATA / "about-cases.json")
    results = []
    validators = {"memory": validate_memories, "portrait": validate_portrait, "month": validate_month}
    for case in corpus["cases"]:
        rejected, certainty = False, None
        try:
            output = validators[case["kind"]](case["response"], case["context"])
            if case["kind"] == "portrait" and output["items"]:
                certainty = output["items"][0]["certainty"]
        except AIError:
            rejected = True
        passed = rejected == case["expect_reject"] and (case["expect_certainty"] is None or certainty == case["expect_certainty"])
        results.append({"id": case["id"], "passed": passed, "rejected": rejected, "certainty": certainty})
    return {"mode": "contract", "suite": "about", "app_version": __version__, "git": revision(),
            "corpus": {"version": corpus["version"], "sha256": digest(DATA / "about-cases.json")},
            "prompts": check_prompts(), "scorer_version": "about-contract.v1", "passed": all(r["passed"] for r in results),
            "notice": "手写响应的离线合约检查；不代表真实模型的理解准确率。", "cases": results}


def main():
    report = evaluate()
    folder = ROOT / ".eval-results"
    folder.mkdir(exist_ok=True)
    target = folder / ("about-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8] + ".json")
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Personal archive contracts: " + str(sum(r["passed"] for r in report["cases"])) + "/" + str(len(report["cases"])))
    print("Report: " + str(target))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write("\n## 关于我 · 离线检查\n\n" + report["notice"] + "\n\n")
            for result in report["cases"]:
                stream.write("- " + result["id"] + ": " + ("通过" if result["passed"] else "失败") + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
