"""Execution boundaries, fail-closed gates and optional judge integration."""
# ruff: noqa: PLR6301

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.outputs import LLMResult
from pydantic import ValidationError
import pytest
from test_collaboration import task
from test_human_review import cross_plan, review_flow

from data_agent.evaluation.grading import grade
from data_agent.evaluation.models import Expectations, Observation
from data_agent.harness import AgentHarness, RuntimePolicy
from data_agent.harness.budget import (
    Budget,
    BudgetExceeded,
    current_budget,
    invoke_model,
)
from data_agent.harness.guardrails import after_query, before_query
from data_agent.harness.judge import AnswerJudge, JudgeCriteria
from data_agent.harness.regression import QualityGate, evaluate_gate
from data_agent.harness.tracing import TraceCollector
from data_agent.models.collaboration import CollaborationPlan
from data_agent.models.outputs import QueryResult
from data_agent.nodes.data_nodes import DataAgentNodes
from data_agent.nodes.visualization import VisualizationNode


@pytest.mark.asyncio
async def test_real_model_is_stopped_before_second_call():
    model = FakeListChatModel(responses=["first", "second"])

    class Flow:
        async def run(self, **kwargs):
            for _ in range(2):
                await model.ainvoke("q", config={"callbacks": kwargs["callbacks"]})
            return {}

    result = await AgentHarness(Flow(), RuntimePolicy(max_llm_calls=1)).run("q")
    assert result.status == "budget_exceeded"
    assert result.budget["llm_calls"] == 1
    assert model.i == 1


def test_usage_limits_and_deduplication():
    budget = Budget(RuntimePolicy(max_tokens=50))
    trace = TraceCollector(10, budget)
    run = uuid4()
    trace.on_chat_model_start({}, [], run_id=run)
    trace.on_llm_start({}, [], run_id=run)
    response = LLMResult(
        generations=[], llm_output={"token_usage": {"total_tokens": 30}}
    )
    trace.on_llm_end(response, run_id=run)
    trace.on_llm_end(response, run_id=run)
    assert budget.tokens == 30
    assert budget.counts["llm_calls"] == 1
    with pytest.raises(BudgetExceeded, match="max_tokens"):
        budget.usage(30, None, None)


def test_unknown_usage_and_cost_pricing():
    with pytest.raises(ValidationError):
        RuntimePolicy(max_cost_usd=1)
    budget = Budget(
        RuntimePolicy(
            max_cost_usd=0.01, input_usd_per_million=1, output_usd_per_million=2
        )
    )
    budget.usage(3000, 1000, 2000)
    assert budget.cost == pytest.approx(0.005)
    with pytest.raises(BudgetExceeded, match="unavailable"):
        budget.usage(None, None, None)


@pytest.mark.asyncio
async def test_disallowed_plan_never_invokes_workers():
    flow, coordinator = review_flow([cross_plan()], policy="never")
    result = await AgentHarness(
        flow, RuntimePolicy(allowed_agents=frozenset({"sales"}))
    ).run("compare")
    assert result.status == "budget_exceeded"
    for worker in coordinator.agents.values():
        worker.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_count_is_checked_before_execution():
    flow, coordinator = review_flow([cross_plan()], policy="never")
    result = await AgentHarness(flow, RuntimePolicy(max_agent_tasks=1)).run("compare")
    assert result.status == "budget_exceeded"
    for worker in coordinator.agents.values():
        worker.ainvoke.assert_not_awaited()


def test_query_restrictions_and_redaction_before_synthesis():
    budget = Budget(
        RuntimePolicy(
            forbidden_sql_columns=frozenset({"ssn"}),
            redact_columns=frozenset({"email"}),
            max_result_rows=1,
        )
    )
    token = current_budget.set(budget)
    try:
        before_query("SELECT COUNT(*) FROM people", "sqlite")
        before_query("SELECT TOP 1 name FROM people", "mssql")
        source = QueryResult(
            columns=["email"], rows=[["a@example.com"], ["b@example.com"]], row_count=2
        )
        result = after_query(source)
        assert result.rows == [["[REDACTED]"]]
        assert result.metadata["truncated"]
        assert source.rows[0][0] == "a@example.com"
        with pytest.raises(BudgetExceeded):
            before_query("SELECT ssn AS innocuous FROM people", "sqlite")
    finally:
        current_budget.reset(token)


def report(mode="live"):
    return {
        "schema_version": 1,
        "suite": "s",
        "data_version": "1",
        "mode": mode,
        "metadata": {},
        "cases": [
            {
                "id": "a",
                "passed": True,
                "elapsed_ms": 10,
                "llm_calls": 2,
                "total_tokens": 50,
                "steps": [
                    {
                        "request": {
                            "expect": {
                                "agents": ["a"],
                                "table": {"columns": ["n"], "rows": [[1]]},
                            }
                        },
                        "observation": {"status": "success"},
                        "passed": True,
                        "failures": [],
                        "infrastructure_error": None,
                    }
                ],
            }
        ],
    }


def test_gate_detects_routing_regression_and_missing_runtime():
    old = report()
    candidate = copy.deepcopy(old)
    candidate["cases"][0]["steps"][0]["failures"] = ["routing: wrong agent"]
    gate = QualityGate(metrics={"routing_accuracy": {"min": 0.9, "max_drop": 0.02}})
    result = evaluate_gate(gate, candidate, old)
    assert not result["passed"]
    assert result["candidate"]["routing_accuracy"] == 0
    assert not evaluate_gate(
        QualityGate(metrics={"p95_ms": {"max": 100}}), report("replay")
    )["passed"]
    candidate["metadata"]["harness_policy"] = {"max_llm_calls": 1}
    with pytest.raises(ValueError, match="harness_policy"):
        evaluate_gate(gate, candidate, old)


@pytest.mark.asyncio
async def test_judge_scores_use_thresholds_and_evidence():
    structured = SimpleNamespace(
        ainvoke=AsyncMock(
            return_value={
                "correctness": 0.8,
                "groundedness": 0.2,
                "completeness": 1,
                "reason": "Missing evidence",
            }
        )
    )
    model = SimpleNamespace(with_structured_output=lambda _schema: structured)
    outcome, failures = await AnswerJudge(model).evaluate(
        "count",
        {"answer": "10", "table": {"rows": [[2]]}, "warnings": []},
        JudgeCriteria(reference_answer="2"),
    )
    assert outcome["scores"]["groundedness"] == 0.2
    assert failures == ["judge groundedness: 0.2 below 0.9"]
    assert "untrusted" in structured.ainvoke.call_args.args[0][0].content


@pytest.mark.asyncio
async def test_zero_tool_budget_prevents_database_execution():
    nodes = object.__new__(DataAgentNodes)
    nodes._datasource = SimpleNamespace()
    nodes._is_cosmos = False
    nodes._dialect = "sqlite"
    nodes._execute_sql = Mock()
    token = current_budget.set(Budget(RuntimePolicy(max_tool_calls=0)))
    try:
        with pytest.raises(BudgetExceeded):
            await nodes.execute_query({"generated_sql": "SELECT 1"})
        nodes._execute_sql.assert_not_called()
    finally:
        current_budget.reset(token)


@pytest.mark.asyncio
async def test_local_code_is_blocked_before_executor():
    executor = type("LocalExecutor", (), {})()
    executor.execute = AsyncMock()
    model = SimpleNamespace(
        ainvoke=AsyncMock(
            return_value=SimpleNamespace(
                content="```python\nimport matplotlib.pyplot as plt\n```"
            )
        )
    )
    token = current_budget.set(Budget(RuntimePolicy()))
    try:
        result = await VisualizationNode(model, executor).generate_visualization({
            "question": "chart",
            "result": {"columns": ["n"], "rows": [[1]]},
        })
        assert "disabled" in result["visualization_error"]
        executor.execute.assert_not_awaited()
    finally:
        current_budget.reset(token)


@pytest.mark.asyncio
async def test_model_deadline_cancels_and_records_timeout():
    model = SimpleNamespace(ainvoke=AsyncMock(side_effect=lambda _: asyncio.sleep(10)))

    async def slow(_messages):
        await asyncio.sleep(10)

    model.ainvoke = slow
    budget = Budget(RuntimePolicy(model_timeout=0.01))
    token = current_budget.set(budget)
    try:
        with pytest.raises(TimeoutError):
            await invoke_model(model, [])
        assert budget.model_timeouts == 1
    finally:
        current_budget.reset(token)


@pytest.mark.asyncio
async def test_failure_trace_is_exported_without_input(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_AGENT_HARNESS_TRACE_DIR", str(tmp_path))

    class BrokenFlow:
        async def run(self, **_kwargs):
            raise RuntimeError("private failure")

    with pytest.raises(RuntimeError):
        await AgentHarness(BrokenFlow()).run("private question")
    paths = list(tmp_path.glob("*.json"))
    assert len(paths) == 1
    content = paths[0].read_text(encoding="utf-8")
    assert "private" not in content
    assert json.loads(content)["status"] == "failed"


def test_planning_assertions_require_matching_dependency_graph():
    expected = Expectations(
        status="success", task_dependencies={"a": [], "b": ["a"]}, join_keys=["id"]
    )
    actual = Observation(
        status="success", task_dependencies={"a": [], "b": []}, join_keys=["other"]
    )
    assert grade(expected, actual) == [
        "planning: task dependencies differ",
        "planning: join keys differ",
    ]


@pytest.mark.asyncio
async def test_policy_concurrency_limits_independent_tasks():
    flow, coordinator = review_flow(
        [CollaborationPlan(tasks=[task("sales"), task("stock")])], policy="never"
    )
    active = 0
    peak = 0

    async def execute(*_args, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"result": QueryResult(columns=["n"], rows=[[1]], row_count=1)}

    for worker in coordinator.agents.values():
        worker.ainvoke.side_effect = execute
    result = await AgentHarness(flow, RuntimePolicy(max_concurrency=1)).run("compare")
    assert result.status == "completed"
    assert peak == 1
    assert result.budget["agent_tasks"] == 2


@pytest.mark.asyncio
async def test_task_timeout_blocks_dependent_worker():
    flow, coordinator = review_flow([cross_plan()], policy="never")

    async def slow(*_args, **_kwargs):
        await asyncio.Event().wait()

    coordinator.agents["sales"].ainvoke.side_effect = slow
    result = await AgentHarness(flow, RuntimePolicy(task_timeout=0.01)).run("compare")
    tasks = result.output["agent_results"]
    assert tasks[0].status == "timeout"
    assert tasks[1].status == "blocked"
    coordinator.agents["stock"].ainvoke.assert_not_awaited()
