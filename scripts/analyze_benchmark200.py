"""Summarize a completed live run without modifying benchmark expectations."""

import argparse
from collections import Counter
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def analyze(report_path):
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report["mode"] != "live":
        raise ValueError("This analysis requires actual live model execution")
    with (ROOT / "evals/benchmark200/coverage.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        coverage = {row["id"]: row for row in csv.DictReader(stream)}
    cases = report["cases"]
    if set(coverage) != {case["id"] for case in cases}:
        raise ValueError("Expected exactly the complete 200-case benchmark")
    breakdowns = {}
    for key in ("split", "kind", "domain", "family"):
        rows = []
        for value in sorted({coverage[case["id"]][key] for case in cases}):
            selected = [case for case in cases if coverage[case["id"]][key] == value]
            passed = sum(case["passed"] for case in selected)
            rows.append({
                "name": value,
                "passed": passed,
                "total": len(selected),
                "pass_rate": passed / len(selected),
            })
        breakdowns[key] = rows
    failures = []
    failure_counts = Counter()
    for case in cases:
        labels = set()
        for step in case["steps"]:
            if not step["passed"]:
                for failure in step["failures"]:
                    labels.add(failure.split(":", 1)[0])
                failures.append({
                    "case_id": case["id"],
                    "step": step["step"],
                    "domain": coverage[case["id"]]["domain"],
                    "split": coverage[case["id"]]["split"],
                    "question": step["request"].get("question")
                    or json.dumps(step["request"].get("resume"), ensure_ascii=False),
                    "expected_status": step["request"]["expect"]["status"],
                    "actual_status": (step.get("observation") or {}).get(
                        "status", "missing"
                    ),
                    "infrastructure_error": step.get("infrastructure_error") or "",
                    "failures": " | ".join(step["failures"]),
                })
        failure_counts.update(labels)
    output = report_path.parent
    with (output / "failures.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "case_id",
                "step",
                "domain",
                "split",
                "question",
                "expected_status",
                "actual_status",
                "infrastructure_error",
                "failures",
            ],
        )
        writer.writeheader()
        writer.writerows(failures)
    summary = report["summary"]
    analysis = {
        "summary": summary,
        "breakdowns": breakdowns,
        "failure_case_counts": dict(failure_counts),
        "failed_case_ids": [case["id"] for case in cases if not case["passed"]],
    }
    (output / "analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 200 案例真实模型评测结果",
        "",
        "固定本地样例数据，真实模型调用；包含 150 个单轮查询、30 个同域多轮任务切换、20 个协作审批场景。",
        "",
        f"完整案例通过：**{summary['passed']}/{summary['total']}（{summary['pass_rate']:.1%}）**。",
        "",
        "| 指标 | 本轮结果 |",
        "|---|---:|",
    ]
    for label, key in [
        ("P50 案例耗时", "p50_ms"),
        ("P95 案例耗时", "p95_ms"),
        ("模型调用", "llm_calls"),
        ("查询执行节点次数", "query_steps"),
        ("模型 tokens", "total_tokens"),
    ]:
        value = summary.get(key)
        rendered = (
            "未知"
            if value is None
            else f"{value / 1000:.2f} 秒"
            if key.endswith("_ms")
            else f"{value:,}"
        )
        lines.append(f"| {label} | {rendered} |")
    for label, key in [
        ("路由断言正确率", "routing_accuracy"),
        ("结果表断言正确率", "result_accuracy"),
        ("超时率", "timeout_rate"),
    ]:
        value = summary.get("harness_metrics", {}).get(key)
        lines.append(
            f"| {label} | {'未知' if value is None else format(value, '.1%')} |"
        )
    for title, key in [
        ("开发与验收", "split"),
        ("案例类型", "kind"),
        ("数据源领域", "domain"),
        ("题目家族", "family"),
    ]:
        lines.extend([
            "",
            "## " + title,
            "",
            "| 分组 | 通过 | 通过率 |",
            "|---|---:|---:|",
        ])
        for row in breakdowns[key]:
            lines.append(
                f"| {row['name']} | {row['passed']}/{row['total']} | {row['pass_rate']:.1%} |"
            )
    lines.extend([
        "",
        "## 失败分析",
        "",
        "以下按包含该类失败的案例计数，同一案例可能有多类失败。",
        "",
    ])
    if failure_counts:
        for label, count in failure_counts.most_common():
            lines.append(f"- {label}：{count} 例。")
        lines.extend([
            "",
            "逐步骤诊断见本目录 `failures.csv`，完整请求、期望、结果与 Trace 见 `report.html` / `report.json`。",
        ])
    else:
        lines.append("本轮没有失败案例。")
    lines.extend([
        "",
        "## 评测口径",
        "",
        "使用 always 审阅策略，每次审批均由题集显式编写；不按模型输出修改标准答案。完整案例需所有步骤和断言通过。路由和结果正确率分别仅统计配置了对应断言的步骤，分母与案例通过率不同。耗时包含初始化、恢复和清理。",
        "",
        "200 个场景不等于 200 个独立 SQL 问题；多轮和交互复用部分单轮查询。开发集与验收集参考 SQL 无重复，但使用相同样例域，不是公开未见域基准。此轮未启用答案 Judge、线上负载或生产数据库测试。没有配置模型费率，不报告实际账单金额。",
        "",
        "与原 30 题使用的 cross_source 策略不同，不能直接声称准确率提升。此报告只描述本轮真实运行，重复运行可测稳定性。",
    ])
    (output / "BENCHMARK.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(analysis, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    analyze(parser.parse_args().report)
