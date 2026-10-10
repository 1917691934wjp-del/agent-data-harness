"""Portable JSON and HTML reports, with all user/model text HTML-escaped."""

# ruff: noqa: RUF001 - Chinese interface punctuation

from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from html import escape
import json
import math
from pathlib import Path
from typing import Any

from data_agent.evaluation.models import CaseResult, EvalSuite
from data_agent.harness.regression import metrics as gate_metrics


def json_text(value: object) -> str:
    def fallback(item: object) -> object:
        if isinstance(item, Decimal):
            return float(item)
        return str(item)

    return json.dumps(
        value, ensure_ascii=False, indent=2, default=fallback, allow_nan=False
    )


def make_report(
    suite: EvalSuite, results: list[CaseResult], mode: str, metadata: dict[str, Any]
) -> dict[str, Any]:
    categories = {}
    for category in sorted({r.category for r in results}):
        cases = [r for r in results if r.category == category]
        categories[category] = {
            "passed": sum(r.passed for r in cases),
            "total": len(cases),
        }
    failures = Counter(
        f.split(":", 1)[0]
        for case in results
        for step in case.steps
        for f in step.failures
    )
    durations = sorted(r.elapsed_ms for r in results)
    live = mode == "live"
    summary = {
        "total": len(results),
        "passed": sum(r.passed for r in results),
        "pass_rate": sum(r.passed for r in results) / len(results) if results else 0,
        "categories": categories,
        "failures": dict(failures),
        "p50_ms": durations[math.ceil(len(durations) * 0.5) - 1]
        if live and durations
        else None,
        "p95_ms": durations[math.ceil(len(durations) * 0.95) - 1]
        if live and durations
        else None,
        "llm_calls": sum(r.llm_calls for r in results)
        if live and all(r.llm_calls is not None for r in results)
        else None,
        "query_steps": sum(r.query_steps for r in results)
        if live and all(r.query_steps is not None for r in results)
        else None,
        "total_tokens": sum(r.total_tokens for r in results)
        if live and all(r.total_tokens is not None for r in results)
        else None,
    }
    report = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "suite": suite.name,
        "data_version": suite.data_version,
        "mode": mode,
        "metadata": metadata,
        "summary": summary,
        "cases": [r.model_dump() for r in results],
    }
    if results:
        report["summary"]["harness_metrics"] = gate_metrics(report)
    return report


def save_report(report: dict[str, Any], directory: Path) -> tuple[Path, Path]:  # noqa: PLR0914 - self-contained report rendering
    directory.mkdir(parents=True, exist_ok=True)
    json_path, html_path = directory / "report.json", directory / "report.html"
    json_path.write_text(json_text(report), encoding="utf-8")
    summary = report["summary"]
    replay = report["mode"] == "replay"
    cards = []
    for case in report["cases"]:
        details = []
        for step in case["steps"]:
            actual = step.get("observation") or {}
            details.append(
                f"<h4>步骤 {step['step']}</h4><p>{escape(str(step['request'].get('question') or step['request'].get('resume')))}</p>"
                + "<ul>"
                + "".join(f"<li>{escape(f)}</li>" for f in step["failures"])
                + "</ul>"
                + f"<div class='compare'><section><h5>预期</h5><pre>{escape(json_text(step['request']['expect']))}</pre></section>"
                + f"<section><h5>实际</h5><pre>{escape(json_text(actual))}</pre></section></div>"
            )
        state = "pass" if case["passed"] else "fail"
        badge = "通过" if case["passed"] else "未通过"
        duration = "回放" if replay else f"{case['elapsed_ms'] / 1000:.2f} 秒"
        cards.append(
            f"<details class='case' data-state='{state}' data-category='{escape(case['category'], quote=True)}'>"
            + f"<summary><span class='badge {state}'>{badge}</span><b>{escape(case['id'])}</b>"
            + f"<span>{escape(case['category'])}</span><span>{duration}</span></summary>"
            + "".join(details)
            + f"<h4>执行事件</h4><pre>{escape(json_text(case['events']))}</pre>"
            + f"<h4>Harness Trace 与预算</h4><pre>{escape(json_text(case.get('harness_runs', [])))}</pre>"
            + f"<h4>可选答案评分</h4><pre>{escape(json_text([s.get('judge_result') for s in case['steps'] if s.get('judge_result')]))}</pre></details>"
        )
    options = "".join(f"<option>{escape(c)}</option>" for c in summary["categories"])
    breakdown = "".join(
        f"<tr><td>{escape(c)}</td><td>{s['passed']} / {s['total']}</td></tr>"
        for c, s in summary["categories"].items()
    )
    note = (
        "离线回放：用于验证评分与报告，不代表真实模型准确率；回放耗时不作为性能指标。"
        if replay
        else "真实执行：结果取决于当前模型、配置和数据快照；状态通过不等于答案内容已被完整验证。"
    )
    p95 = "—" if summary["p95_ms"] is None else f"{summary['p95_ms'] / 1000:.2f}s"
    html = """<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Agent 评测中心</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f5f8;color:#172033;font:15px/1.6 'Segoe UI','Microsoft YaHei',sans-serif}main{max-width:1180px;margin:36px auto;padding:0 24px}h1{font-size:30px;margin-bottom:4px}h2{font-size:19px}h4{margin-bottom:8px}h5{margin:8px 0}.muted{color:#56657a}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:24px 0}.metric,.panel{background:white;border:1px solid #dfe5ec;border-radius:12px;padding:18px}.metric strong{display:block;font-size:29px}table{border-collapse:collapse;width:100%}td{border-bottom:1px solid #e9edf2;padding:8px}input,select{padding:10px;border:1px solid #c6cfda;border-radius:7px;font:inherit}.filters{display:flex;gap:12px;margin:20px 0;flex-wrap:wrap}input{flex:1;min-width:200px}.case{background:white;border:1px solid #dfe5ec;border-radius:10px;margin:12px 0;padding:14px 18px}summary{cursor:pointer;display:flex;gap:20px;align-items:center;flex-wrap:wrap}summary b{flex:1}.badge{padding:2px 10px;border-radius:14px;font-size:13px}.pass{color:#116445;background:#e4f5ec}.fail{color:#a83131;background:#fdecec}.compare{display:grid;grid-template-columns:1fr 1fr;gap:16px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f8fb;padding:14px;border-radius:8px;font:12px/1.6 Consolas,monospace;max-height:450px;overflow:auto}section{min-width:0}[hidden]{display:none!important}@media(max-width:700px){.metrics,.compare{grid-template-columns:1fr 1fr}main{padding:0 12px}.compare{grid-template-columns:1fr}}
</style><main><div class='muted'>DATA AGENT / EVALUATION</div><h1>Agent 评测中心</h1>"""
    html += f"<p>{escape(report['suite'])} · {escape(report['data_version'])}</p><p class='muted'>{note}</p>"
    html += f"<div class='metrics'><div class='metric'>案例数<strong>{summary['total']}</strong></div><div class='metric'>通过案例<strong>{summary['passed']}</strong></div><div class='metric'>案例通过率<strong>{summary['pass_rate']:.1%}</strong></div><div class='metric'>P95 耗时<strong>{p95}</strong></div></div>"
    html += f"<div class='panel'><h2>分类结果</h2><table>{breakdown}</table><details><summary>运行元信息与调用统计</summary><pre>{escape(json_text({'metadata': report['metadata'], 'summary': summary}))}</pre></details></div>"
    diagnostics = "".join(
        f"<tr><td>{escape(key)}</td><td>{'未知 / 未评测' if value is None else escape(str(round(value, 4)))}</td></tr>"
        for key, value in summary.get("harness_metrics", {}).items()
    )
    html += f"<div class='panel'><h2>质量与运行指标</h2><table>{diagnostics}</table><p class='muted'>路由与结果准确率仅统计配置了对应断言的步骤；回放不提供模型性能指标。费用与 Judge 调用统计在各步骤中单独记录。</p></div>"
    html += (
        f"<h2>案例明细</h2><div class='filters'><input id='search' aria-label='搜索案例' placeholder='搜索问题、SQL 或失败原因'><select id='state' aria-label='状态'><option value='all'>全部状态</option><option value='fail'>仅未通过</option><option value='pass'>仅通过</option></select><select id='category' aria-label='分类'><option value='all'>全部分类</option>{options}</select></div><p id='count' class='muted'></p>"
        + "".join(cards)
    )
    html += """</main><script>
const search=document.getElementById('search'),state=document.getElementById('state'),category=document.getElementById('category');
function filter(){let count=0;document.querySelectorAll('.case').forEach(el=>{const show=(state.value==='all'||el.dataset.state===state.value)&&(category.value==='all'||el.dataset.category===category.value)&&el.textContent.toLowerCase().includes(search.value.toLowerCase());el.hidden=!show;if(show)count++});document.getElementById('count').textContent=`显示 ${count} 个案例`}
[search,state,category].forEach(el=>el.addEventListener('input',filter));filter();</script></html>"""
    html_path.write_text(html, encoding="utf-8")
    return json_path, html_path
