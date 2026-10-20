"""Replay both organization policies through real queues in isolated synthetic stores."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import inspect
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

from app import __version__
from app.ai import AIError, ModelClient, Worker, validate_config
from app.about import encode, signature
from app.memory_selection import SELECTION_VERSION
from app.organization import ORGANIZATION_VERSION
from app.request_context import CONTEXT_VERSION
from app.store import Store
from app.usage import CALL_SCOPE, UsageLedger, provider_key, rate
from evals.run import DATA, check_prompts, revision

REPORT_VERSION = "pipeline-report.v1"
POLICIES = {"always": "始终 AI（保留现有缓存与输入精简）", "auto": "按需整理（生成前判断、生成后呈现）"}


class CallBudget:
    def __init__(self, limit):
        self.limit, self.used, self.exhausted = limit, 0, False

    def reserve(self):
        if self.used >= self.limit:
            self.exhausted = True
            raise AIError("已达到本次实验的请求上限")
        self.used += 1


class ContractClient:
    """Deliberately repetitive test double. Its output is not a quality benchmark."""
    def __init__(self, ledger):
        self.ledger = ledger

    @staticmethod
    def growth(record):
        return {"schema_version": 1, "memories": [{
            "title": "一次表达的具体经历", "detail": record["body"], "category": "fact",
            "horizon": "recent", "period": record["period"],
            "sources": [{"record_id": record["id"], "quote": record["body"]}],
        }], "events": [{"title": "一次主动表达", "detail": record["body"], "kind": "moment", "quote": record["body"]}]}

    def call(self, settings, prompt, content):
        identifier = self.ledger.start(settings)
        try:
            if "entry" in content:
                entry = content["entry"]
                result = {"schema_version": 1, "summary": entry["body"], "insights": [], "actions": [], "completions": []}
                if content.get("growth_enabled"):
                    result["growth"] = self.growth({"id": content["growth_record_id"], "body": entry["body"], "period": entry["journal_day"]})
                return result
            if "part" in content:
                return self.growth(content["records"][0])
            if "memories" in content:
                memories = content["memories"][:2]
                return {"schema_version": 1, "items": [{"text": memories[0]["detail"], "category": "focus",
                    "certainty": "hypothesis", "memory_ids": [m["id"] for m in memories]}]}
            if "month" in content:
                record = next(r for r in content["records"] if r["period"].startswith(content["month"]))
                return {"schema_version": 1, "items": [{"text": record["body"], "category": "events", "comparison": False,
                    "sources": [{"record_id": record["id"], "quote": record["body"]}]}]}
            record = content["records"][0]
            return {"schema_version": 1, "overview": record["body"], "sections": [{"category": "insights", "items": [
                {"text": record["body"], "sources": [{"entry_id": record["id"], "quote": record["body"]}]}]}]}
        finally:
            # Unknown, not invented token counts. No network request was made.
            self.ledger.finish(identifier, {}, "contract-fixture", "success", 0)


class ObservedClient:
    def __init__(self, backend, budget, mode):
        self.backend, self.budget, self.mode = backend, budget, mode
        self.ledger, self.calls = backend.ledger, []
        self.phase = "records"

    def call(self, settings, prompt, content):
        self.budget.reserve()
        started = time.perf_counter()
        call = {"kind": CALL_SCOPE.get().get("kind", "other"), "phase": self.phase, "attempt": CALL_SCOPE.get().get("attempt", 1),
                "request_characters": len(prompt) + len(encode(content)), "output": None, "error": None}
        self.calls.append(call)
        try:
            call["output"] = self.backend.call(settings, prompt, content)
            return deepcopy(call["output"])
        except Exception as exc:
            call["error"] = type(exc).__name__  # Never retain provider error bodies or credentials.
            raise
        finally:
            call["latency_ms"] = round((time.perf_counter() - started) * 1000, 2) if self.mode == "live" else None


def drain(store, worker, budget):
    for _ in range(200):
        # Simulate waiting for debounce/retry deadlines. Do not change queue logic.
        with store.connection() as db:
            db.execute("UPDATE jobs SET retry_at=NULL WHERE status='queued'")
        if not worker.step() or budget.exhausted:
            return
    raise RuntimeError("合成流程未能收敛")


def metrics(calls, rows, mode):
    result = {"calls": len(calls), "by_kind": dict(sorted(Counter(c["kind"] for c in calls).items())),
              "by_phase": dict(sorted(Counter(c.get("phase", "records") for c in calls).items())),
              "retry_calls": sum(c["attempt"] > 1 for c in calls),
              "request_characters": sum(c["request_characters"] for c in calls)}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        result[key] = sum(r[key] for r in rows) if mode == "live" and len(rows) == len(calls) and all(r[key] is not None for r in rows) else None
    costs = [Decimal(r["estimated_cost"]) for r in rows if r["estimated_cost"] is not None]
    result["known_cost_cny"] = format(sum(costs, Decimal(0)), "f") if mode == "live" else None
    result["estimated_cost_cny"] = result["known_cost_cny"] if mode == "live" and len(costs) == len(calls) else None
    result["unknown_cost_calls"] = len(calls) - len(costs)
    result["latency_ms"] = round(sum(c["latency_ms"] for c in calls), 2) if mode == "live" else None
    return result


def replay(scenario, policy, mode, settings, key, prices, budget, backend_factory=None):
    with tempfile.TemporaryDirectory(prefix="shiguang-eval-") as directory:
        store, ledger = Store(directory), UsageLedger(directory)
        store.save_settings({**settings, "organization_mode": policy})
        settings = store.settings()
        if prices:
            ledger.save_price(settings, {"model": settings["ai_model"], "provider": provider_key(settings), **prices})
        if scenario.get("growth") and not scenario.get("history"):
            store.set_growth_policy({"growth_enabled": True, "growth_daily_calls": 100, "growth_monthly_budget": "10000"})
        class EnvironmentKey:
            def get(self):
                return key
        backend = backend_factory(ledger) if backend_factory else ModelClient(EnvironmentKey(), ledger) if mode == "live" else ContractClient(ledger)
        client = ObservedClient(backend, budget, mode)
        client.phase = "historical_setup" if scenario.get("history") else "records"
        worker = Worker(store, client, recap_delay=0)
        entries = []
        for index, record in enumerate(scenario["records"]):
            entries.append(store.save_entry({"id": f"pipeline-{scenario['id']}-{index}", "body": record["body"], "journal_day": record["day"]}))
            drain(store, worker, budget)
            if budget.exhausted:
                break
        if scenario.get("history") and not budget.exhausted:
            client.phase = "backfill"
            store.set_growth_policy({"growth_enabled": True, "growth_daily_calls": 100, "growth_monthly_budget": "10000"})
            drain(store, worker, budget)
        if scenario.get("edit") and not budget.exhausted:
            client.phase = "edit"
            entries[0] = store.save_entry({**entries[0], "body": scenario["edit"]}, entries[0]["id"])
            drain(store, worker, budget)
        before_rechecks = len(client.calls)
        client.phase = "recheck"
        for _ in range(scenario.get("rechecks", 0)):
            if budget.exhausted:
                break
            for entry in entries:
                store.retry(entry_id=entry["id"], force=False)
            for day in sorted({e["journal_day"] for e in entries}):
                store.retry(day=day, force=False)
            if scenario.get("growth"):
                store.request_about("memory")
            drain(store, worker, budget)
        after_rechecks = len(client.calls)
        if scenario.get("month") and not budget.exhausted:
            client.phase = "month"
            try:
                store.request_about("month", scenario["month"])
                drain(store, worker, budget)
            except ValueError:
                pass  # Insufficient live-model evidence remains visible in outputs/checks.
        outputs = {day: store.summary(day) for day in sorted({e["journal_day"] for e in entries})}
        with ledger.connection() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM calls ORDER BY rowid")]
        for call, row in zip(client.calls, rows):
            call["accounting"] = {name: row[name] for name in (
                "input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens",
                "estimated_cost", "cost_basis", "input_rate", "output_rate", "cached_rate", "status")}
        with store.connection() as db:
            unfinished = db.execute("SELECT count(*) FROM jobs WHERE status!='done'").fetchone()[0]
            jobs = [dict(r) for r in db.execute("SELECT kind,target,status,attempts,error FROM jobs ORDER BY kind,target")]
            actual = {r["id"]: dict(r) for r in db.execute("SELECT * FROM entries")}
        checks = {"originals_preserved": len(entries) == len(scenario["records"]) and all(actual.get(e["id"], {}).get("body") == e["body"] for e in entries),
                  "queue_finished": unfinished == 0 and not budget.exhausted,
                  "requests_accounted": len(rows) == len(client.calls) and all(r["status"] != "running" for r in rows),
                  "cache_rechecks_without_calls": after_rechecks == before_rechecks}
        if scenario.get("growth"):
            outputs["about"] = store.about_state(scenario.get("month", "2026-10"))
            checks["separate_growth_occurrences"] = store.timeline()["total"] == len(scenario["records"])
            if scenario.get("month"):
                checks["requested_month_report_available"] = "month" in outputs["about"]["reports"]
        return {"id": scenario["id"], "title": scenario["title"], "policy": policy,
                "checks": checks, "passed": all(checks.values()), "metrics": metrics(client.calls, rows, mode),
                "calls": client.calls, "outputs": outputs, "jobs": jobs, "budget_exhausted": budget.exhausted,
                "accounting_warning": ledger.warning or None}


def evaluate(mode="contract", settings=None, key="", prices=None, case_ids=None, max_calls=100, backend_factory=None):
    if mode not in ("contract", "live") or isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
        raise ValueError("请选择有效模式和正整数请求上限")
    corpus = json.loads((DATA / "pipeline-cases.json").read_text(encoding="utf-8"))
    selected = [s for s in corpus["scenarios"] if case_ids is None or s["id"] in case_ids]
    if not selected or case_ids is not None and set(case_ids) != {s["id"] for s in selected}:
        raise ValueError("合成流程 ID 不存在")
    if mode == "live":
        validate_config(settings or {})
    else:
        settings = {"ai_mode": "local", "ai_base_url": "http://127.0.0.1:1/v1", "ai_model": "contract-fixture"}
    budget, results = CallBudget(max_calls), []
    for scenario in selected:
        for policy in POLICIES:
            results.append(replay(scenario, policy, mode, settings, key, prices, budget, backend_factory))
            if budget.exhausted:
                break
        if budget.exhausted:
            break
    totals = {}
    for policy in POLICIES:
        group = [r for r in results if r["policy"] == policy]
        names = ("calls", "retry_calls", "request_characters", "input_tokens", "output_tokens", "total_tokens", "latency_ms")
        total = {name: sum(r["metrics"][name] for r in group) if group and all(r["metrics"][name] is not None for r in group) else None for name in names}
        costs = [r["metrics"]["estimated_cost_cny"] for r in group]
        total["estimated_cost_cny"] = format(sum((Decimal(c) for c in costs), Decimal(0)), "f") if group and all(c is not None for c in costs) else None
        total["unknown_cost_calls"] = sum(r["metrics"]["unknown_cost_calls"] for r in group)
        total["by_kind"] = dict(sum((Counter(r["metrics"]["by_kind"]) for r in group), Counter()))
        total["by_phase"] = dict(sum((Counter(r["metrics"]["by_phase"]) for r in group), Counter()))
        totals[policy] = total
    comparable = len(results) == len(selected) * 2 and not budget.exhausted
    passed = comparable and all(r["passed"] for r in results)
    old, new = totals["always"]["estimated_cost_cny"], totals["auto"]["estimated_cost_cny"]
    reduction = round(float((Decimal(old) - Decimal(new)) / Decimal(old) * 100), 2) if passed and old is not None and new is not None and Decimal(old) > 0 else None
    return {"report_version": REPORT_VERSION, "mode": mode, "app_version": __version__, "git": revision(),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "corpus": {"version": corpus["version"], "sha256": signature(corpus)}, "selected_ids": [s["id"] for s in selected],
            "fixture_sha256": signature(inspect.getsource(ContractClient).replace("\r\n", "\n")) if mode == "contract" else None,
            "prompts": check_prompts(), "context_version": CONTEXT_VERSION, "organization_version": ORGANIZATION_VERSION,
            "selection_version": SELECTION_VERSION, "model": settings["ai_model"], "provider": provider_key(settings),
            "prices": prices, "policies": POLICIES, "totals": totals, "samples": results,
            "schedule": "每次保存后推进至队列静止，模拟记录之间已超过自动回顾的等待窗口；不衡量快速连续输入的合并收益。",
            "comparable": comparable, "cost_reduction_percent": reduction,
            "passed": passed, "max_calls": max_calls,
            "notice": "手写替身只验证调用路径；字符数不是 Token、费用或模型质量。" if mode == "contract" else
                      "全部为合成记录；费用按提供方用量与输入单价估算，未知不当作零。结果仍需人工审阅；单次实验不能证明长期收益。"}


def compare_reports(baseline, candidate):
    reasons = []
    for name in ("report_version", "mode", "corpus", "selected_ids"):
        if baseline.get(name) != candidate.get(name):
            reasons.append("不可直接比较：" + name + " 不同")
    if baseline.get("mode") == "contract" and baseline.get("fixture_sha256") != candidate.get("fixture_sha256"):
        reasons.append("不可直接比较：手写替身不同")
    if not baseline.get("comparable") or not candidate.get("comparable"):
        reasons.append("不可直接比较：存在未完成的配对流程")
    for kind, before in baseline.get("prompts", {}).items():
        after = candidate.get("prompts", {}).get(kind, {})
        if before.get("version") == after.get("version") and before.get("sha256") != after.get("sha256"):
            reasons.append("不可直接比较：提示词复用旧版本覆盖内容 " + kind)
    if reasons:
        return {"comparable": False, "passed": False, "reasons": reasons, "deltas": {}}
    old = {(s["id"], s["policy"]): s for s in baseline["samples"]}
    for sample in candidate["samples"]:
        previous = old.get((sample["id"], sample["policy"]))
        if previous:
            for name, passed in previous["checks"].items():
                if passed and not sample["checks"].get(name):
                    reasons.append(sample["id"] + " / " + sample["policy"] + "：" + name + " 退步")
    if not candidate["passed"]:
        reasons.append("候选流程检查未通过")
    deltas = {}
    for policy in POLICIES:
        before, after = baseline["totals"][policy], candidate["totals"][policy]
        deltas[policy] = {name: after[name] - before[name] if before[name] is not None and after[name] is not None else None
                          for name in ("calls", "input_tokens", "output_tokens", "total_tokens")}
        old_cost, new_cost = before["estimated_cost_cny"], after["estimated_cost_cny"]
        deltas[policy]["estimated_cost_cny"] = format(Decimal(new_cost) - Decimal(old_cost), "f") if old_cost is not None and new_cost is not None else None
    return {"comparable": True, "passed": not reasons, "reasons": reasons, "deltas": deltas}


def summary(report):
    rows = ["# 拾光完整流程对比", "", report["notice"], "",
            "基线使用同一版本的缓存和输入精简，仅关闭按需整理的生成前判断与生成后呈现。两边分别使用空白临时数据库。",
            report["schedule"],
            "覆盖单条、每日回顾、成长提取、画像、月度回顾及重试；本机判断和缓存命中不会虚构模型请求。", "",
            "| 方案 | 请求 | 输入 Token | 输出 Token | 总 Token | 估算费用 / 元 |",
            "| --- | --- | --- | --- | --- | --- |"]
    for policy, label in POLICIES.items():
        value = report["totals"][policy]
        fields = [value[k] if value[k] is not None else "未知" for k in ("calls", "input_tokens", "output_tokens", "total_tokens", "estimated_cost_cny")]
        rows.append("| " + " | ".join([label] + [str(v) for v in fields]) + " |")
    rows += ["", "| 阶段 | 始终 AI 请求 | 按需请求 |", "| --- | --- | --- |"]
    for phase, label in (("records", "新增记录"), ("historical_setup", "历史日记的前期整理"), ("backfill", "首次补处理历史日记"), ("edit", "修改原文"), ("recheck", "重复检查"), ("month", "月度回顾")):
        rows.append("| " + " | ".join([label] + [str(report["totals"][p]["by_phase"].get(phase, 0)) for p in POLICIES]) + " |")
    rows += ["", "总计为从空白开始重放的生命周期开销。历史日记前期整理属于模拟的已发生成本；升级后的增量开销应查看补处理历史与后续操作，不能把已发生的费用算作可追回的节省。"]
    rows += ["", "| 场景 | 始终 AI 请求 | 按需请求 | 流程检查 |", "| --- | --- | --- | --- |"]
    for identifier in report["selected_ids"]:
        samples = {s["policy"]: s for s in report["samples"] if s["id"] == identifier}
        rows.append("| " + " | ".join([next(iter(samples.values()))["title"]] +
                    [str(samples[p]["metrics"]["calls"]) if p in samples else "未完成" for p in POLICIES] +
                    ["通过" if len(samples) == 2 and all(s["passed"] for s in samples.values()) else "未通过"]) + " |")
    rows += ["", "可比性：" + ("完整配对" if report["comparable"] else "实验未完成，不能比较总成本"),
             "费用变化：" + (f"降低 {report['cost_reduction_percent']}%" if report["cost_reduction_percent"] is not None else "未知；不以字符数或请求次数冒充费用节省"),
             "", "生成后呈现判断在本机执行，不额外请求模型，也不会退还本次生成已消耗的 Token。服务方的输入缓存可能影响两边费用，请查看逐次缓存用量并重复实验。",
             "", "流程检查只证明原文保存、队列、缓存和独立成长记录的机制。请结合原有行动质量评测，人工检查摘要、画像及月度回顾是否忠实。", ""]
    if report.get("comparison"):
        result = report["comparison"]
        rows += ["与历史报告比较：" + ("通过" if result["passed"] else "未通过"),
                 "；".join(result["reasons"]), "", "变化量（候选减基线；未知费用不计算差额）：", "",
                 "```json", json.dumps(result["deltas"], ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(rows)


def main():
    # Redirected Windows terminals can default to cp1252 even when the report is
    # Chinese. Use a stable UTF-8 contract for both console output and error text.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("contract", "live"), default="contract")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--allow-cloud", action="store_true")
    parser.add_argument("--key-env", default="DIARY_EVAL_API_KEY")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--max-calls", type=int, default=100, help="total request cap across both policies, including retries")
    parser.add_argument("--input-price")
    parser.add_argument("--output-price")
    parser.add_argument("--cached-price")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / ".eval-results")
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    try:
        settings, key, prices = None, "", None
        if args.mode == "live":
            if not args.model or not args.base_url:
                raise ValueError("真实实验需要 --model 和 --base-url")
            settings = {"ai_mode": "cloud" if args.allow_cloud else "local", "ai_model": args.model,
                        "ai_base_url": args.base_url, "cloud_consent": args.allow_cloud}
            key = os.environ.get(args.key_env, "")
            if args.allow_cloud and not key:
                raise ValueError("请先在本机终端设置密钥环境变量")
        if args.input_price is not None or args.output_price is not None or args.cached_price is not None:
            prices = {"input_rate": rate(args.input_price), "output_rate": rate(args.output_price), "cached_rate": rate(args.cached_price, True)}
        report = evaluate(args.mode, settings, key, prices, args.case_ids, args.max_calls)
        if args.baseline:
            report["comparison"] = compare_reports(json.loads(args.baseline.read_text(encoding="utf-8")), report)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        target = args.output_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-pipeline-" + uuid.uuid4().hex[:8] + ".json")
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        target.with_suffix(".md").write_text(summary(report), encoding="utf-8")
        print(summary(report))
        print("Report: " + str(target))
        return 0 if report["passed"] and report.get("comparison", {"passed": True})["passed"] else 1
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
