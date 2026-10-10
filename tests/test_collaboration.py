"""Offline behavior tests for parallel tasks, evidence exchange and safe joins."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from langchain_community.utilities import SQLDatabase
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langgraph.types import Command
from pydantic import ValidationError
import pytest
from sqlalchemy import create_engine, text

from data_agent.agent import DataAgentFlow
from data_agent.collaboration import AgentCoordinator, join_results
from data_agent.config import AgentConfig, DataAgentConfig
from data_agent.graph import create_data_agent
from data_agent.models.collaboration import (
    AgentTask,
    AgentTaskResult,
    CollaborationPlan,
    CollaborationSettings,
    JoinSpec,
)
from data_agent.models.outputs import (
    QueryResult,
    ResponseGeneratorOutput,
    SQLGeneratorOutput,
)
from data_agent.nodes.data_nodes import DataAgentNodes


def task(name, agent=None, deps=()):
    return AgentTask(
        task_id=name,
        agent_name=agent or name,
        question=f"Query {name}",
        depends_on=list(deps),
    )


def table(rows=None):
    rows = rows if rows is not None else [["A", 12]]
    return QueryResult(columns=["product_id", "value"], rows=rows, row_count=len(rows))


def evidence(name, rows=None, status="success"):
    return AgentTaskResult(
        task_id=name,
        agent_name=name,
        question=f"Query {name}",
        status=status,
        result=table(rows),
        error=None if status == "success" else "offline",
    )


def worker(output=None):
    return SimpleNamespace(
        ainvoke=AsyncMock(
            return_value=output
            or {
                "result": table(),
                "generated_sql": "SELECT 1",
                "final_response": "ok",
            }
        )
    )


def coordinator(agents, plans=None, settings=None):
    planner = SimpleNamespace(ainvoke=AsyncMock(side_effect=plans))
    llm = SimpleNamespace(
        with_structured_output=Mock(return_value=planner),
        ainvoke=AsyncMock(return_value=AIMessage(content="Combined answer")),
    )
    return AgentCoordinator(
        llm,
        agents,
        {name: name for name in agents},
        settings or CollaborationSettings(review_policy="never"),
    )


def flow_for(coord):
    flow = DataAgentFlow.__new__(DataAgentFlow)
    flow._enable_collaboration = True
    flow._collaboration_settings = coord.settings
    flow.data_agents = coord.agents
    flow._intent_llm = coord.llm
    flow._agent_descriptions = coord.catalog
    flow.config = AgentConfig(
        data_agents=[DataAgentConfig(name=name) for name in coord.agents]
    )
    flow._callback = BaseCallbackHandler()
    flow._graph = flow._build_workflow()
    return flow


@pytest.mark.asyncio
async def test_independent_tasks_really_overlap():
    both_started = asyncio.Event()
    started = []

    async def invoke(state, _config):
        started.append(state["datasource_name"])
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        return {"result": table()}

    coord = coordinator({
        name: SimpleNamespace(ainvoke=invoke) for name in ["sales", "stock"]
    })
    output = await coord.execute_tasks(
        {"collaboration_plan": CollaborationPlan(tasks=[task("sales"), task("stock")])},
        {},
    )
    assert [r.status for r in output["agent_results"]] == ["success", "success"]


@pytest.mark.asyncio
async def test_dependent_task_receives_structured_evidence_not_shared_state():
    source, target = worker(), worker()
    coord = coordinator({"sales": source, "stock": target})
    plan = CollaborationPlan(tasks=[task("sales"), task("stock", deps=["sales"])])
    await coord.execute_tasks(
        {"collaboration_plan": plan}, {"configurable": {"thread_id": "parent"}}
    )
    state, config = target.ainvoke.call_args.args
    payload = json.loads(state["collaboration_context"])
    assert payload[0]["task_id"] == "sales"
    assert payload[0]["result"]["rows"] == [["A", 12]]
    assert "configurable" not in config
    assert "result" not in state
    assert state["skip_response"] is True


@pytest.mark.asyncio
async def test_failure_blocks_only_dependents_and_retains_independent_results():
    broken = worker()
    broken.ainvoke.side_effect = RuntimeError("offline")
    dependent = worker()
    coord = coordinator({"bad": broken, "dependent": dependent, "good": worker()})
    plan = CollaborationPlan(
        tasks=[task("bad"), task("dependent", deps=["bad"]), task("good")]
    )
    output = await coord.execute_tasks({"collaboration_plan": plan}, {})
    assert [r.status for r in output["agent_results"]] == [
        "error",
        "blocked",
        "success",
    ]
    dependent.ainvoke.assert_not_awaited()
    summary = await coord.summarize_results({
        "question": "compare",
        "collaboration_plan": plan,
        **output,
    })
    assert summary["error"] is None
    assert "offline" in summary["final_response"]
    assert summary["result"] is None


@pytest.mark.asyncio
async def test_timeout_does_not_lose_successful_result():
    async def hang(_state, _config):
        await asyncio.Event().wait()

    coord = coordinator(
        {"slow": SimpleNamespace(ainvoke=hang), "fast": worker()},
        settings=CollaborationSettings(task_timeout=0.02),
    )
    plan = CollaborationPlan(tasks=[task("slow"), task("fast")])
    output = await coord.execute_tasks({"collaboration_plan": plan}, {})
    assert [r.status for r in output["agent_results"]] == ["timeout", "success"]


@pytest.mark.asyncio
async def test_concurrency_bound():
    active = 0
    peak = 0

    async def invoke(_state, _config):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return {"result": table()}

    coord = coordinator(
        {"db": SimpleNamespace(ainvoke=invoke)},
        settings=CollaborationSettings(max_concurrency=2),
    )
    plan = CollaborationPlan(tasks=[task(f"t{i}", "db") for i in range(5)])
    await coord.execute_tasks({"collaboration_plan": plan}, {})
    assert peak == 2


@pytest.mark.parametrize(
    "tasks",
    [
        [task("a"), task("a")],
        [task("a", deps=["missing"])],
        [task("a", deps=["a"])],
        [task("a", deps=["b"]), task("b", deps=["a"])],
    ],
)
def test_invalid_plans_never_execute(tasks):
    with pytest.raises(ValidationError):
        CollaborationPlan(tasks=tasks)


@pytest.mark.asyncio
async def test_unknown_agent_fails_closed():
    w = worker()
    coord = coordinator(
        {"known": w}, plans=[CollaborationPlan(tasks=[task("unknown")])]
    )
    flow = flow_for(coord)
    output = await flow.run("query", thread_id="unknown")
    assert output["error"] == "planning_failed"
    w.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_flow_parallel_then_single_turn_clears_old_outputs():
    plan = CollaborationPlan(
        tasks=[task("sales"), task("stock")],
        join=JoinSpec(task_ids=["sales", "stock"], keys=["product_id"]),
    )
    coord = coordinator(
        {"sales": worker(), "stock": worker()},
        plans=[plan, CollaborationPlan(tasks=[task("stock")])],
    )
    flow = flow_for(coord)
    first = await flow.run("compare", thread_id="same")
    assert len(first["agent_results"]) == 2
    assert first["result"].columns == ["product_id", "sales.value", "stock.value"]
    second = await flow.run("stock only", thread_id="same")
    assert len(second["agent_results"]) == 1
    assert second["datasource_name"] == "stock"
    assert second["result"].columns == ["product_id", "value"]
    assert second["final_response"] == "ok"
    assert second["collaboration_warnings"] == []


@pytest.mark.asyncio
async def test_clarification_can_resume_without_executing_early():
    clarification = CollaborationPlan(clarification="Which time range?")
    coord = coordinator(
        {"sales": worker()},
        plans=[clarification, CollaborationPlan(tasks=[task("sales")])],
    )
    flow = flow_for(coord)
    first = await flow.run("sales", thread_id="clarify")
    assert first["__interrupt__"][0].value["message"] == "Which time range?"
    coord.agents["sales"].ainvoke.assert_not_awaited()
    second = await flow.run(
        thread_id="clarify", resume_value={"question": "Sales last week"}
    )
    assert second["final_response"] == "ok"


def test_join_outer_and_typed_keys_preserve_missing_values():
    spec = JoinSpec(task_ids=["a", "b"], keys=["product_id"])
    result = join_results(
        spec,
        [evidence("a", [["A", 10], [1, 3]]), evidence("b", [["A", 4], ["1", 5]])],
        20,
    )
    assert result.rows == [["A", 10, 4], [1, 3, None], ["1", None, 5]]


@pytest.mark.parametrize("rows", [[["A", 1], ["A", 2]], [[None, 1]], [[{}, 1]]])
def test_join_refuses_ambiguous_or_null_keys(rows):
    with pytest.raises(ValueError):
        join_results(
            JoinSpec(task_ids=["a", "b"], keys=["product_id"]),
            [evidence("a", rows), evidence("b")],
            10,
        )


def test_join_refuses_missing_columns_and_excess_rows():
    spec = JoinSpec(task_ids=["a", "b"], keys=["product_id"])
    missing = evidence("b")
    missing.result.columns = ["other", "value"]
    with pytest.raises(ValueError, match="missing join keys"):
        join_results(spec, [evidence("a"), missing], 10)
    with pytest.raises(ValueError, match="row limit"):
        join_results(spec, [evidence("a", [["A", 1], ["B", 2]]), evidence("b")], 1)


@pytest.mark.asyncio
async def test_join_failure_and_synthesis_failure_preserve_evidence():
    coord = coordinator({"a": worker(), "b": worker()})
    coord.llm.ainvoke.side_effect = RuntimeError("LLM offline")
    plan = CollaborationPlan(
        tasks=[task("a"), task("b")],
        join=JoinSpec(task_ids=["a", "b"], keys=["product_id"]),
    )
    result = await coord.summarize_results({
        "question": "compare",
        "collaboration_plan": plan,
        "agent_results": [evidence("a", [["A", 1], ["A", 2]]), evidence("b")],
    })
    assert result["result"] is None
    assert len(result["collaboration_warnings"]) == 2
    assert "duplicate join keys" in result["final_response"]


@pytest.mark.asyncio
async def test_dependency_overflow_is_not_silently_truncated():
    coord = coordinator(
        {"a": worker(), "b": worker()},
        settings=CollaborationSettings(max_context_chars=1000),
    )
    large = evidence("a", [["A", "x" * 2000]])
    result = await coord._run_task(
        task("b", deps=["a"]), {"a": large}, {}, [], multiple=True
    )
    assert result.status == "error"
    coord.agents["b"].ainvoke.assert_not_awaited()


def make_db(path):
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE products (product_id TEXT, value INTEGER)")
        )
        connection.execute(text("INSERT INTO products VALUES ('A', 12), ('B', 7)"))
    return SQLDatabase(engine)


@pytest.mark.asyncio
async def test_real_sqlite_returns_typed_rows_and_empty_columns(tmp_path):
    db = make_db(tmp_path / "source.db")
    nodes = DataAgentNodes(Mock(), db, DataAgentConfig(name="db"))
    full = await nodes.execute_query({
        "generated_sql": "SELECT product_id, value FROM products ORDER BY product_id"
    })
    assert full["result"].rows == [["A", 12], ["B", 7]]
    assert full["result"].columns == ["product_id", "value"]
    empty = await nodes.execute_query({
        "generated_sql": "SELECT product_id FROM products WHERE 1=0"
    })
    assert empty["result"].columns == ["product_id"]
    assert empty["result"].row_count == 0


@pytest.mark.asyncio
async def test_actual_worker_graphs_join_two_sqlite_sources(tmp_path):
    agents = {}
    for name in ["sales", "stock"]:
        db = make_db(tmp_path / f"{name}.db")
        structured = SQLGeneratorOutput(
            thinking="",
            sql_query="SELECT product_id, value FROM products",
            explanation="query",
        )
        llm = Mock()
        llm.with_structured_output.return_value = SimpleNamespace(
            ainvoke=AsyncMock(return_value=structured)
        )
        agents[name] = create_data_agent(
            llm, db, DataAgentConfig(name=name), checkpointer=False
        )
    plan = CollaborationPlan(
        tasks=[task("sales"), task("stock")],
        join=JoinSpec(task_ids=["sales", "stock"], keys=["product_id"]),
    )
    coord = coordinator(agents, plans=[plan])
    result = await flow_for(coord).run("Compare sales and stock", thread_id="sqlite")
    assert result["error"] is None
    assert result["result"].rows == [["A", 12, 12], ["B", 7, 7]]
    assert all(r.status == "success" for r in result["agent_results"])


@pytest.mark.asyncio
async def test_worker_receives_dependency_context_on_generation_and_retry(tmp_path):
    llm = Mock()
    generator = SimpleNamespace(
        ainvoke=AsyncMock(
            return_value=SQLGeneratorOutput(
                thinking="", sql_query="SELECT 1", explanation="ok"
            )
        )
    )
    llm.with_structured_output.return_value = generator
    nodes = DataAgentNodes(
        llm, make_db(tmp_path / "db.sqlite"), DataAgentConfig(name="db")
    )
    state = {
        "question": "lookup",
        "collaboration_context": '[{"id":"A"}]',
        "skip_visualization": True,
    }
    await nodes.generate_sql(state)
    await nodes.retry_sql({**state, "error": "syntax"})
    for call in generator.ainvoke.call_args_list:
        assert any('[{"id":"A"}]' in msg.content for msg in call.args[0])


@pytest.mark.asyncio
async def test_legacy_router_and_real_single_worker_still_respond(tmp_path):
    db = make_db(tmp_path / "legacy.sqlite")
    sql = SQLGeneratorOutput(
        thinking="",
        sql_query="SELECT value FROM products WHERE product_id='A'",
        explanation="lookup",
    )
    llm = Mock()
    llm.with_structured_output.side_effect = [
        SimpleNamespace(ainvoke=AsyncMock(return_value=sql)),
        SimpleNamespace(
                ainvoke=AsyncMock(return_value=ResponseGeneratorOutput(response="Value is 12"))
        ),
    ]
    agent = create_data_agent(llm, db, DataAgentConfig(name="sales"))
    coord = coordinator({"sales": agent})
    flow = flow_for(coord)
    flow._enable_collaboration = False
    flow._intent_llm.ainvoke = AsyncMock(
        side_effect=[AIMessage(content="sales"), AIMessage(content="Value for A")]
    )
    flow._graph = flow._build_workflow()
    result = await flow.run("Value for A", thread_id="legacy")
    assert result["final_response"] == "Value is 12"
    assert result["result"].rows == [[12]]
    assert result["agent_results"] == []


@pytest.mark.asyncio
async def test_same_worker_graph_tasks_do_not_overwrite_each_other(tmp_path):
    db = make_db(tmp_path / "shared.sqlite")

    def generate(messages):
        question = messages[-1].content
        sql = (
            "SELECT product_id, value FROM products WHERE product_id='A'"
            if "first" in question
            else "SELECT product_id, value FROM products WHERE product_id='B'"
        )
        return SQLGeneratorOutput(
            thinking="",
            sql_query=sql,
            explanation="lookup",
        )

    llm = Mock()
    llm.with_structured_output.return_value = SimpleNamespace(
        ainvoke=AsyncMock(side_effect=generate)
    )
    agent = create_data_agent(llm, db, DataAgentConfig(name="db"), checkpointer=False)
    plan = CollaborationPlan(tasks=[task("first", "db"), task("second", "db")])
    coord = coordinator({"db": agent}, plans=[plan])
    output = await flow_for(coord).run("Two separate lookups", thread_id="shared")
    assert [r.result.rows for r in output["agent_results"]] == [[["A", 12]], [["B", 7]]]


@pytest.mark.asyncio
async def test_failed_turn_clears_previous_successful_answer_and_table():
    w = worker()
    coord = coordinator(
        {"a": w}, plans=[CollaborationPlan(tasks=[task("a")]), RuntimeError("offline")]
    )
    flow = flow_for(coord)
    first = await flow.run("first", thread_id="repeat")
    assert first["result"] is not None
    second = await flow.run("second", thread_id="repeat")
    assert second["result"] is None
    assert second["agent_results"] == []
    assert not second["generated_sql"]
    assert second["error"] == "planning_failed"


@pytest.mark.asyncio
async def test_all_tasks_fail_does_not_call_summarizer():
    coord = coordinator({"a": worker(), "b": worker()})
    plan = CollaborationPlan(tasks=[task("a"), task("b")])
    result = await coord.summarize_results({
        "question": "compare",
        "collaboration_plan": plan,
        "agent_results": [
            evidence("a", status="error"),
            evidence("b", status="timeout"),
        ],
    })
    assert result["error"] == "all_tasks_failed"
    coord.llm.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_propagates():
    w = worker()
    w.ainvoke.side_effect = asyncio.CancelledError()
    coord = coordinator({"a": w})
    with pytest.raises(asyncio.CancelledError):
        await coord.execute_tasks(
            {"collaboration_plan": CollaborationPlan(tasks=[task("a")])}, {}
        )


@pytest.mark.asyncio
async def test_empty_result_is_success_but_absent_result_is_error():
    coord = coordinator({
        "empty": worker({"result": table([])}),
        "missing": worker({"final_response": "no data"}),
    })
    plan = CollaborationPlan(tasks=[task("empty"), task("missing")])
    result = await coord.execute_tasks({"collaboration_plan": plan}, {})
    assert [r.status for r in result["agent_results"]] == ["success", "error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer", [{"question": None}, {"question": ""}, {"question": "still unclear"}]
)
async def test_unresolved_clarification_stops_without_worker_calls(answer):
    clarification = CollaborationPlan(clarification="Which data?")
    coord = coordinator({"a": worker()}, plans=[clarification, clarification])
    flow = flow_for(coord)
    result = await flow.run("unclear", thread_id="clarify")
    assert "__interrupt__" in result
    result = await flow._graph.ainvoke(
        Command(resume=answer), {"configurable": {"thread_id": "clarify"}}
    )
    assert result["error"] == "out_of_scope"
    coord.agents["a"].ainvoke.assert_not_awaited()


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda r: r.result.metadata.update(truncated=True), "truncated"),
        (
            lambda r: setattr(r.result, "columns", ["product_id", "product_id"]),
            "duplicate column",
        ),
        (lambda r: setattr(r.result, "rows", [["A"]]), "malformed"),
        (lambda r: setattr(r, "status", "error"), "did not succeed"),
    ],
)
def test_join_refuses_unusable_source_data(mutation, message):
    first = evidence("a")
    mutation(first)
    with pytest.raises(ValueError, match=message):
        join_results(
            JoinSpec(task_ids=["a", "b"], keys=["product_id"]),
            [first, evidence("b")],
            10,
        )


def test_summary_preview_is_labelled_without_mutating_full_result():
    source = evidence("a", [["A", "x" * 200], ["B", "y" * 200]])
    preview = AgentCoordinator._table_preview(source.result, 250)
    assert preview["preview_truncated"] is True
    assert preview["row_count"] == 2
    assert preview["shown_rows"] == 1
    assert len(source.result.rows) == 2


@pytest.mark.asyncio
async def test_sql_row_cap_marks_truncation(tmp_path):
    cfg = DataAgentConfig(name="db")
    cfg.validation_config.max_rows = 1
    nodes = DataAgentNodes(Mock(), make_db(tmp_path / "limit.sqlite"), cfg)
    result = await nodes.execute_query({
        "generated_sql": "SELECT product_id, value FROM products"
    })
    assert result["result"].row_count == 1
    assert result["result"].metadata["truncated"] is True
