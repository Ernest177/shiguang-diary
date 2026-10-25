"""Offline by default. Live mode sends ONLY the committed synthetic corpus."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone
import uuid

from app import __version__
from app.ai import ModelClient, REQUEST_OPTIONS, validate_config
from app.request_options import request_options
from app.source_refs import SOURCE_REFS_VERSION
from app.prompts import ENTRY_PROMPT, DAY_PROMPT, prompt_metadata
from app.request_context import CONTEXT_VERSION, prepare_request
from evals.scoring import SCORER_VERSION, aggregate, gate, score_case

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "evals" / "corpus"
REPORT_VERSION = 1
GATE_VERSION = "quality.v1"


def digest(path):
    # Canonical JSON makes identities independent of checkout line endings.
    return hashlib.sha256(json.dumps(read_json(path), ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def check_prompts():
    registry = read_json(DATA / "prompt-registry.json")
    current = prompt_metadata()
    for value in current.values():
        if registry.get(value["version"]) != value["sha256"]:
            raise ValueError("提示词未登记或旧版本内容改变：" + value["version"])
    return current


def revision():
    def git(*args):
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def evaluate(mode="contract", settings=None, key="", case_ids=None):
    corpus = read_json(DATA / "cases.json")
    fixtures = read_json(DATA / "contract-responses.json")
    selected = [case for case in corpus["cases"] if case_ids is None or case["id"] in case_ids]
    if not selected or (case_ids is not None and set(case_ids) != {c["id"] for c in selected}):
        raise ValueError("样例 ID 不存在或未选择样例")
    if mode not in ("contract", "live"):
        raise ValueError("未知评测模式")
    prompts = check_prompts()
    if mode == "live":
        validate_config(settings or {})
    class EnvironmentKey:
        def get(self):
            return key
    client = ModelClient(EnvironmentKey()) if mode == "live" else None
    samples = []
    for case in selected:
        started = time.perf_counter()
        output = None
        error = None
        metadata = {}
        wire_output = None
        try:
            if mode == "contract":
                output = fixtures[case["id"]]
            else:
                prompt = ENTRY_PROMPT if case["kind"] == "entry" else DAY_PROMPT
                payload, decode = prepare_request(case["kind"], case["input"])
                wire_output, metadata = client.call_with_metadata(settings, prompt, payload)
                output = decode(wire_output)
        except Exception as exc:
            # Exception types only. Never persist provider error bodies or credentials.
            error = type(exc).__name__
        elapsed = round((time.perf_counter() - started) * 1000, 2) if mode == "live" else None
        samples.append({"id": case["id"], "kind": case["kind"], "tags": case["tags"],
                        "error": error, "latency_ms": elapsed, "provider": metadata,
                        "wire_output": wire_output,
                        "output": output, "score": score_case(case, output)})
    metrics = aggregate(samples)
    latencies = sorted(s["latency_ms"] for s in samples if s["latency_ms"] is not None)
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    usage_samples = 0
    for sample in samples:
        value = sample["provider"].get("usage", {})
        if all(isinstance(value.get(k), int) and not isinstance(value[k], bool) and value[k] >= 0 for k in usage):
            usage_samples += 1
            for key in usage:
                usage[key] += value[key]
    return {"report_version": REPORT_VERSION, "mode": mode,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "app_version": __version__, "git": revision(), "prompts": prompts,
            "corpus": {"version": corpus["version"], "sha256": digest(DATA / "cases.json")},
            "fixture_sha256": digest(DATA / "contract-responses.json") if mode == "contract" else None,
            "scorer_version": SCORER_VERSION, "gate_version": GATE_VERSION, "source_reference_version": SOURCE_REFS_VERSION,
            "selected_ids": [c["id"] for c in selected],
            "model": settings["ai_model"] if mode == "live" else "handwritten-contract-fixture",
            "request_options": {**request_options(settings), "response_format": None, "enable_thinking": request_options(settings).get("enable_thinking"), "context_version": CONTEXT_VERSION},
            "metrics": metrics, "gate": gate(metrics),
            "performance": {"latency_samples": len(latencies),
                            "p50_ms": latencies[(len(latencies) - 1) // 2] if latencies else None,
                            "p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * .95))] if latencies else None,
                            "usage_samples": usage_samples, "usage": usage if usage_samples else None,
                            "estimated_cost_cny": None}, "samples": samples}


def summary(report):
    m = report["metrics"]
    label = "离线合约回归（手写替身，不代表模型质量）" if report["mode"] == "contract" else "真实模型 · 合成样例评测"
    def percent(value):
        return "—" if value is None else f"{value:.1%}"
    rows = ["# 拾光评测报告", "", label, "",
            f"应用 {report['app_version']} · 数据集 {report['corpus']['version']} · 评分器 {report['scorer_version']}",
            f"模型：{report['model']}；样例：{m['cases']}；门禁：{'通过' if report['gate']['passed'] else '未通过'}", "",
            "| 指标 | 原始输出 | 应用校验后 |", "| --- | --- | --- |"]
    for name, key in (("行动精确率", "precision"), ("行动召回率", "recall"), ("行动 F1", "f1"),
                      ("匹配行动的日期正确率", "date_accuracy"), ("已有行动关联正确率", "link_accuracy"),
                      ("明确完成召回率", "completion_recall")):
        rows.append(f"| {name} | {percent(m['raw'][key])} | {percent(m['final'][key])} |")
    for name, key in (("误提待办数", "critical_false_tasks"), ("误关闭数", "unexpected_completions")):
        rows.append(f"| {name} | {m['raw'].get(key, 0)} | {m['final'].get(key, 0)} |")
    rows.extend(["", f"结构通过率 {percent(m['schema_valid_rate'])}；原始引用有效率 {percent(m['raw_quote_valid_rate'])}；每日来源覆盖 {percent(m['day_source_coverage'])}。",
                 f"请求失败 {m['request_errors']}；延迟与用量：`{json.dumps(report['performance'], ensure_ascii=False)}`。", "",
                 "## 逐例检查", "", "| 样例 | 结构 | 行动遗漏 / 误提 | 误关闭 |", "| --- | --- | --- | --- |"])
    for sample in report["samples"]:
        score = sample["score"]
        final = score.get("final", {})
        rows.append(f"| {sample['id']} | {'通过' if score['schema_valid'] else '失败'} | {final.get('fn', 0)} / {final.get('fp', 0)} | {final.get('unexpected_completions', 0)} |")
    if report["gate"]["reasons"]:
        rows.extend(["", "未通过原因：" + "；".join(report["gate"]["reasons"])])
    if "comparison" in report:
        result = report["comparison"]
        rows.extend(["", "基线比较：" + ("通过" if result["passed"] else "未通过"),
                     "；".join(result["reasons"])])
    rows.extend(["", "行动按引用锚点、类别与标题表达式匹配；引用存在不证明结论正确。每日覆盖不评价摘要语义质量。发布前仍需人工复核漏项、事实支持、任务状态及建议边界。", ""])
    return "\n".join(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("contract", "live"), default="contract")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--key-env", default="DIARY_EVAL_API_KEY", help="environment variable name; never pass the key itself")
    parser.add_argument("--allow-cloud", action="store_true", help="allow sending synthetic cases to HTTPS service")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".eval-results")
    parser.add_argument("--input-price", type=float, help="CNY per million input tokens, explicit estimate only")
    parser.add_argument("--output-price", type=float, help="CNY per million output tokens")
    args = parser.parse_args()
    settings = None
    key = ""
    try:
        if args.mode == "live":
            if not args.model or not args.base_url:
                raise ValueError("真实评测需要 --model 和 --base-url")
            settings = {"ai_mode": "cloud" if args.allow_cloud else "local", "ai_model": args.model,
                        "ai_base_url": args.base_url, "cloud_consent": args.allow_cloud}
            validate_config(settings)
            key = os.environ.get(args.key_env, "")
            if args.allow_cloud and not key:
                raise ValueError("密钥环境变量尚未设置；请在本机终端设置，不要发到聊天或写入仓库")
        if any(p is not None and (p < 0 or not math.isfinite(p)) for p in (args.input_price, args.output_price)):
            raise ValueError("估算单价须为有限非负数字")
        report = evaluate(args.mode, settings, key, args.case_ids)
        perf = report["performance"]
        if perf["usage_samples"] == report["metrics"]["cases"] and args.input_price is not None and args.output_price is not None:
            perf["estimated_cost_cny"] = round((perf["usage"]["prompt_tokens"] * args.input_price +
                                               perf["usage"]["completion_tokens"] * args.output_price) / 1_000_000, 6)
            perf["price_assumption"] = {"input_cny_per_million": args.input_price, "output_cny_per_million": args.output_price}
        if args.baseline:
            from evals.compare import compare
            report["comparison"] = compare(read_json(args.baseline), report)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        filename = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        target = args.output_dir / (filename + ".json")
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        markdown = summary(report)
        target.with_suffix(".md").write_text(markdown, encoding="utf-8")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
                stream.write(markdown)
        print(f"{args.mode}: {report['metrics']['cases']} cases; gate={'PASS' if report['gate']['passed'] else 'FAIL'}")
        print("Report: " + str(target))
        comparison = report.get("comparison", {"passed": True})
        if not comparison["passed"]:
            print("Comparison: " + "; ".join(comparison["reasons"]))
        return 0 if report["gate"]["passed"] and comparison["passed"] else 1
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
