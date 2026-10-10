"""Offline evaluation contracts, runner isolation and compiled HITL integration."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from langchain_core.outputs import LLMResult
from pydantic import ValidationError
import pytest
from test_human_review import cross_plan, review_flow
from typer.testing import CliRunner

from data_agent.evaluation.cli import app, live_factory, load_replay, read_suite
from data_agent.evaluation.grading import compare_table
from data_agent.evaluation.metrics import EvalMetrics
from data_agent.evaluation.models import (
    EvalCase,
    EvalStep,
    EvalSuite,
    ExpectedTable,
    Observation,
    ResumeInput,
)
from data_agent.evaluation.report import make_report, save_report
from data_agent.evaluation.runner import LiveSession, ReplaySession, run_case, run_suite

SAMPLE = Path(__file__).resolve().parents[1] / "evals" / "sample"


def case(name="example", status="success"):
    return EvalCase(
        id=name,
        category="test",
        steps=[EvalStep(question="count", expect={"status": status})],
    )


@pytest.mark.parametrize(
    "data",
    [
        {"action": "revise"},
        {"action": "approve", "question": "yes"},
        {"action": "approve", "feedback": "text"},
        {"question": "   "},
    ],
)
def test_invalid_resume_is_rejected(data):
    with pytest.raises(ValidationError):
        ResumeInput.model_validate(data)


def test_schema_rejects_duplicates_typos_and_bad_rows():
    with pytest.raises(ValidationError, match="unique"):
        EvalSuite(name="s", data_version="v", cases=[case(), case()])
    with pytest.raises(ValidationError):
        EvalStep(question="x", expect={"status": "success", "agent": ["sales"]})
    with pytest.raises(ValidationError):
        ExpectedTable(columns=["x"], rows=[[1, 2]])


@pytest.mark.parametrize(
    ("expected", "actual", "valid"),
    [
        ([[1], [1]], [[1], [2]], False),
        ([[1], [2]], [[2], [1]], True),
        ([[None]], [[0]], False),
        ([[1]], [["1"]], False),
        ([[1]], [[True]], False),
        ([[1.0]], [[1]], True),
        ([[9007199254740992]], [[9007199254740993]], False),
    ],
)
def test_table_multiset_and_types(expected, actual, valid):
    spec = ExpectedTable(columns=["x"], rows=expected)
    assert (not compare_table(spec, {"columns": ["x"], "rows": actual})) is valid


def test_table_order_columns_and_overlapping_tolerance():
    spec = ExpectedTable(columns=["x", "y"], rows=[[1, 2]])
    assert not compare_table(spec, {"columns": ["y", "x"], "rows": [[2, 1]]})
    spec = ExpectedTable(columns=["x"], rows=[[0.1], [0]], abs_tol=0.1)
    assert not compare_table(spec, {"columns": ["x"], "rows": [[0], [0.2]]})
    spec = ExpectedTable(columns=["x"], rows=[[1], [2]], ordered=True)
    assert compare_table(spec, {"columns": ["x"], "rows": [[2], [1]]})


@pytest.mark.parametrize(
    "extra",
    [
        {"metadata": {"truncated": True}},
        {"row_count": 2},
        {"rows": [[1, 2]]},
        {"columns": ["x", "x"]},
    ],
)
def test_incomplete_or_malformed_table_cannot_pass(extra):
    assert compare_table(
        ExpectedTable(columns=["x"], rows=[[1]]),
        {"columns": ["x"], "rows": [[1]], **extra},
    )


async def test_case_failure_timeout_and_cleanup_do_not_stop_suite():
    sessions = []

    def factory(item):
        async def execute(_step):
            if item.id == "broken":
                raise RuntimeError("database unavailable")
            if item.id == "slow":
                await asyncio.sleep(1)
            return Observation(status="success")

        session = SimpleNamespace(
            execute=execute, close=AsyncMock(), metrics=lambda: {}
        )
        sessions.append(session)
        return session

    suite = EvalSuite(
        name="s",
        data_version="v",
        cases=[case("broken", "error"), case("slow"), case("ok")],
    )
    results = await run_suite(suite, factory, 0.01)
    assert [r.passed for r in results] == [False, False, True]
    assert results[0].steps[0].infrastructure_error == "RuntimeError"
    assert results[1].steps[0].infrastructure_error == "TimeoutError"
    for session in sessions:
        session.close.assert_awaited_once()


async def test_metrics_failure_still_closes_session():
    def broken():
        raise RuntimeError("callback failed")

    session = SimpleNamespace(
        execute=AsyncMock(return_value=Observation(status="success")),
        metrics=broken,
        close=AsyncMock(),
    )
    result = await run_case(case(), lambda _: session, 1)
    assert not result.passed
    session.close.assert_awaited_once()


async def test_compiled_review_flow_is_paused_until_script_approval():
    flow, coord = review_flow([cross_plan()])
    flow.connect, flow.disconnect = AsyncMock(), AsyncMock()
    flow.datasources = {}
    collector = EvalMetrics()
    flow._extra_callbacks = [collector]
    session = LiveSession(flow, collector)
    first = await session.execute(
        EvalStep(question="compare", expect={"status": "plan_review"})
    )
    assert first.status == "plan_review"
    for worker in coord.agents.values():
        worker.ainvoke.assert_not_awaited()
    approved = await session.execute(
        EvalStep(resume={"action": "approve"}, expect={"status": "success"})
    )
    assert approved.status == "success"
    assert approved.agents == ["sales", "stock"]
    assert collector.snapshot()["events"]  # Observer reaches the real compiled graph.
    for worker in coord.agents.values():
        worker.ainvoke.assert_awaited_once()
    assert LiveSession(flow, collector).thread_id != session.thread_id
    with pytest.raises(ValueError, match="without pending"):
        await session.execute(
            EvalStep(resume={"action": "approve"}, expect={"status": "success"})
        )
    await session.close()


def test_token_metrics_are_deduplicated_and_unknown_is_not_zero():
    collector = EvalMetrics()
    first, second = uuid4(), uuid4()
    collector.on_chat_model_start({}, [], run_id=first)
    collector.on_llm_start({}, [], run_id=first)
    collector.on_llm_end(
        LLMResult(generations=[], llm_output={"token_usage": {"total_tokens": 42}}),
        run_id=first,
    )
    assert collector.snapshot()["total_tokens"] == 42
    assert collector.snapshot()["llm_calls"] == 1
    collector.on_chat_model_start({}, [], run_id=second)
    assert collector.snapshot()["total_tokens"] is None
    query = uuid4()
    collector.on_chain_start(None, {}, run_id=query, name="execute_query")
    collector.on_chain_end({}, run_id=query)
    assert collector.snapshot()["query_steps"] == 1


async def test_html_escapes_model_output_and_replay_has_no_latency(tmp_path):
    suite = EvalSuite(name="s", data_version="v", cases=[case()])
    result = await run_case(
        suite.cases[0],
        lambda _: ReplaySession([
            Observation(status="success", answer="<script>alert(1)</script>")
        ]),
        1,
    )
    report = make_report(suite, [result], "replay", {})
    assert report["summary"]["p95_ms"] is None
    assert report["summary"]["llm_calls"] is None
    _, html = save_report(report, tmp_path)
    content = html.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in content
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content


async def test_cli_demo_and_comparison(tmp_path):
    runner = CliRunner()
    assert runner.invoke(app, ["validate", str(SAMPLE / "suite.json")]).exit_code == 0
    args = [
        "run",
        str(SAMPLE / "suite.json"),
        "--observations",
        str(SAMPLE / "demo_observations.json"),
        "--output",
        str(tmp_path),
    ]
    # CLI owns an asyncio.run loop; isolate it from pytest-asyncio's loop policy.
    result = await asyncio.to_thread(runner.invoke, app, args)
    assert result.exit_code == 1, result.output
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["summary"]["passed"] == 25
    assert report["summary"]["total"] == 30
    assert runner.invoke(app, args).exit_code == 2  # Preserve previous report.
    path = str(tmp_path / "report.json")
    assert runner.invoke(app, ["compare", path, path]).exit_code == 0
    report["data_version"] = "changed"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(report), encoding="utf-8")
    assert runner.invoke(app, ["compare", path, str(changed)]).exit_code == 2


def test_live_approval_requires_explicit_flag_before_loading_config():
    result = CliRunner().invoke(
        app,
        [
            "run",
            str(SAMPLE / "suite.json"),
            "--mode",
            "live",
            "--config",
            "missing-config",
        ],
    )
    assert result.exit_code == 2
    assert "--allow-approvals" in result.output


def test_replay_rejects_missing_steps(tmp_path):
    suite = read_suite(SAMPLE / "suite.json")
    records = json.loads(
        (SAMPLE / "demo_observations.json").read_text(encoding="utf-8")
    )
    records["observations"]["followup"].pop()
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    with pytest.raises(ValueError, match="step count"):
        load_replay(path, suite)


def test_live_config_matches_sample_snapshot_without_model_calls(monkeypatch):
    monkeypatch.setenv("DATA_AGENT_SAMPLE_MODE", "true")
    monkeypatch.setenv(
        "DATA_AGENT_SAMPLE_DIR", str(SAMPLE.parents[1] / "scripts" / "data")
    )
    _, metadata = live_factory("all", "cross_source")
    suite = read_suite(SAMPLE / "suite.json")
    assert metadata["sample_files_sha256"] == suite.data_files_sha256
    assert len(metadata["models"]) == 6


def test_live_rejects_changed_sample_data_before_execution(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "data_agent.evaluation.cli.live_factory",
        lambda *_: (None, {"sample_files_sha256": {}}),
    )
    result = CliRunner().invoke(
        app,
        [
            "run",
            str(SAMPLE / "suite.json"),
            "--mode",
            "live",
            "--config",
            "all",
            "--allow-approvals",
            "--output",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    assert "snapshot" in result.output
