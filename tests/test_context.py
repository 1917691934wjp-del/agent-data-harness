"""Shared live metadata, business definitions and durable confirmation semantics."""

# ruff: noqa: RUF001 - Chinese test inputs

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
import pytest
from test_collaboration import flow_for, task, worker

from data_agent.adapters.sample import create_sample_database
from data_agent.agent import DataAgentFlow
from data_agent.collaboration import AgentCoordinator
from data_agent.config import ColumnSchema, DataAgentConfig, TableSchema
from data_agent.config_loader import ConfigLoader
from data_agent.context import (
    INVENTORY_METRICS,
    ContextUpdate,
    approve_context,
    build_schema_snapshot,
    inventory_metrics,
    prepare_context,
)
from data_agent.models.collaboration import CollaborationPlan, CollaborationSettings
from data_agent.models.outputs import SQLGeneratorOutput
from data_agent.nodes.data_nodes import DataAgentNodes

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database():
    db = create_sample_database(str(ROOT / "scripts/data/postgres_data.json"))
    yield db
    db._engine.dispose()


def plan(*, followup=False, updates=(), clarify=None):
    return CollaborationPlan(
        tasks=[] if clarify else [task("inventory")],
        clarification=clarify,
        context_mode="followup" if followup else "new",
        context_updates=list(updates),
    )


def update(field, value, evidence=""):
    return ContextUpdate(field=field, value=value, evidence=evidence)


def context_flow(plans, saver=None, policy="cross_source"):
    planner = SimpleNamespace(ainvoke=AsyncMock(side_effect=plans))
    llm = SimpleNamespace(
        with_structured_output=Mock(return_value=planner),
        ainvoke=AsyncMock(return_value=AIMessage(content="ok")),
    )
    coord = AgentCoordinator(
        llm,
        {"inventory": worker()},
        {"inventory": "inventory"},
        CollaborationSettings(review_policy=policy),
        metrics={"inventory": list(INVENTORY_METRICS)},
        schema_versions={"inventory": "schema-v1"},
    )
    with patch("data_agent.agent.AgentCoordinator", return_value=coord):
        flow = flow_for(coord)
        if saver:
            flow._checkpointer = saver
            flow._graph = flow._build_workflow()
    return flow, coord


async def decide(flow, first, action, **extra):
    pending = first["__interrupt__"][0].value
    return await flow.run(
        thread_id=first["thread_id"],
        resume_value={
            "action": action,
            "plan_version": pending["plan_version"],
            **extra,
        },
    )


def test_schema_uses_real_columns_and_rejects_stale_annotations(database):
    cfg = DataAgentConfig(
        name="inventory",
        table_schemas=[
            TableSchema(
                name="inventory",
                description="每条库存记录",
                columns=[
                    ColumnSchema(
                        name="quantity", data_type="WRONG", description="实物库存"
                    )
                ],
            )
        ],
    )
    snapshot = build_schema_snapshot(cfg, database)
    assert snapshot.has_columns(
        "inventory", ["inventory_id", "product_id", "warehouse_id", "quantity"]
    )
    assert "实物库存" in snapshot.text and "WRONG" not in snapshot.text
    assert "INSERT" not in snapshot.text
    cfg.table_schemas[0].columns.append(ColumnSchema(name="reserved_quantity"))
    with pytest.raises(ValueError, match="Stale schema"):
        build_schema_snapshot(cfg, database)


def test_sample_planner_and_workers_share_identical_snapshot(monkeypatch):
    monkeypatch.setenv("DATA_AGENT_SAMPLE_MODE", "true")
    cfg = ConfigLoader.load_by_name("contoso")
    snapshots, catalogs = {}, []
    monkeypatch.setattr(
        "data_agent.agent.get_llm",
        lambda **_: SimpleNamespace(with_structured_output=Mock()),
    )

    def make_worker(**kwargs):
        snapshots[kwargs["config"].name] = kwargs["schema_snapshot"]
        return worker()

    def make_coordinator(*args, **kwargs):
        catalogs.append(args[2])
        return AgentCoordinator(*args, **kwargs)

    monkeypatch.setattr("data_agent.agent.create_data_agent", make_worker)
    monkeypatch.setattr("data_agent.agent.AgentCoordinator", make_coordinator)
    flow = DataAgentFlow(config=cfg)
    try:
        assert not cfg.data_agents[-1].table_schemas
        for name, snapshot in snapshots.items():
            assert snapshot is flow._schema_snapshots[name]
            assert snapshot.text in catalogs[0][name]
        assert "reorder_level" in catalogs[0]["contoso_inventory"]
        assert len(inventory_metrics(snapshots["contoso_inventory"])) == 4
        assert not inventory_metrics(snapshots["contoso_sales"])
    finally:
        for db in flow.datasources.values():
            db._engine.dispose()


async def test_generation_and_repair_receive_same_context(database):
    structured = SimpleNamespace(
        ainvoke=AsyncMock(
            return_value=SQLGeneratorOutput(
                sql_query="SELECT COUNT(*) AS total FROM inventory",
                thinking="count",
                explanation="count",
            )
        )
    )
    llm = SimpleNamespace(with_structured_output=Mock(return_value=structured))
    cfg = DataAgentConfig(name="inventory")
    snapshot = build_schema_snapshot(cfg, database)
    nodes = DataAgentNodes(llm, database, cfg, schema_snapshot=snapshot)
    assert nodes._get_schema_context() == snapshot.text
    state = {
        "question": "库存不足记录数",
        "query_context": {"marker": "confirmed-context-test"},
    }
    await nodes.generate_sql(state)
    await nodes.retry_sql({
        **state,
        "error": "wrong SQL",
        "generated_sql": "SELECT wrong",
        "retry_count": 0,
    })
    for call in structured.ainvoke.call_args_list:
        content = str(call.args[0])
        assert "confirmed-context-test" in content
        assert "low_stock_records" in content
        assert snapshot.version in content


async def test_approval_survives_long_history_and_is_forwarded_to_worker():
    initial = plan(
        updates=[
            update("metric_id", "low_stock_records"),
            update("warehouse_scope", "上海仓", "上海仓"),
        ]
    )
    flow, coord = context_flow([initial, *[plan(followup=True) for _ in range(5)]])
    first = await flow.run("上海仓库存不足有多少", thread_id="long")
    payload = first["__interrupt__"][0].value
    assert (
        payload["query_context"]["metric"]["expression"]
        == "COUNT(*) WHERE quantity < reorder_level"
    )
    assert "口径" in payload["message"]
    coord.agents["inventory"].ainvoke.assert_not_awaited()
    done = await decide(flow, first, "approve")
    original = done["confirmed_context"]
    assert original["conditions"]["metric_id"]["status"] == "approved"
    for _ in range(5):
        done = await flow.run("沿用条件再查一次", thread_id="long")
    assert done["query_context"] == original
    worker_input = coord.agents["inventory"].ainvoke.call_args.args[0]
    assert (
        worker_input["query_context"]["conditions"]["warehouse_scope"]["value"]
        == "上海仓"
    )
    assert "上海仓" in str(coord.planner.ainvoke.call_args.args[0])


async def test_revision_is_pending_and_cancel_preserves_confirmed_context():
    first_plan = plan(updates=[update("metric_id", "low_stock_records")])
    flow, _ = context_flow([
        first_plan,
        plan(followup=True, updates=[update("warehouse_scope", "北京仓")]),
        plan(followup=True, updates=[update("warehouse_scope", "上海仓", "上海仓")]),
    ])
    first = await flow.run("查库存不足", thread_id="rev")
    approved = await decide(flow, first, "approve")
    pending = await flow.run("换个仓库", thread_id="rev")
    revision = await decide(flow, pending, "revise", feedback="只看上海仓")
    assert (
        revision["__interrupt__"][0].value["plan_version"]
        != pending["__interrupt__"][0].value["plan_version"]
    )
    assert revision["confirmed_context"] == approved["confirmed_context"]
    cancelled = await decide(flow, revision, "cancel")
    assert cancelled["confirmed_context"] == approved["confirmed_context"]


async def test_new_topic_does_not_inherit_inventory_conditions():
    flow, _ = context_flow([
        plan(updates=[update("warehouse_scope", "上海仓", "上海仓")]),
        plan(),
    ])
    first = await flow.run("查上海仓", thread_id="topic")
    assert (
        first["query_context"]["conditions"]["warehouse_scope"]["status"]
        == "user_stated"
    )
    assert first["confirmed_context"] == {}
    second = await flow.run("查询所有库存记录", thread_id="topic")
    assert second["query_context"]["conditions"] == {}


async def test_clarification_reply_is_saved_as_user_stated():
    flow, _ = context_flow([
        plan(clarify="哪个仓库？"),
        plan(followup=True, updates=[update("warehouse_scope", "上海仓", "上海仓")]),
    ])
    first = await flow.run("查仓库库存", thread_id="clarify-context")
    assert first["__interrupt__"][0].value["type"] == "clarification_needed"
    done = await flow.run(
        thread_id="clarify-context", resume_value={"question": "上海仓"}
    )
    assert (
        done["query_context"]["conditions"]["warehouse_scope"]["evidence"] == "上海仓"
    )


async def test_persistent_approval_restores_after_restart(tmp_path):
    path = str(tmp_path / "context.sqlite")
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        flow, _ = context_flow(
            [plan(updates=[update("metric_id", "low_stock_records")])], saver
        )
        first = await flow.run("查库存不足", thread_id="restart")
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        flow, coord = context_flow([], saver)
        restored = await decide(flow, first, "approve")
        assert restored["confirmed_context"]["metric"]["id"] == "low_stock_records"
        coord.planner.ainvoke.assert_not_awaited()


async def test_schema_change_invalidates_pending_approval():
    flow, coord = context_flow([
        plan(updates=[update("metric_id", "low_stock_records")]),
        plan(followup=True),
    ])
    first = await flow.run("查库存不足", thread_id="stale")
    coord.schema_versions["inventory"] = "schema-v2"
    fresh = await decide(flow, first, "approve")
    assert (
        fresh["__interrupt__"][0].value["plan_version"]
        != first["__interrupt__"][0].value["plan_version"]
    )
    coord.agents["inventory"].ainvoke.assert_not_awaited()
    done = await decide(flow, fresh, "approve")
    assert done["confirmed_context"]["schema_versions"]["inventory"] == "schema-v2"


def test_unknown_metric_and_unquoted_clear_are_rejected():
    kwargs = {
        "followup": True,
        "question": "随便查",
        "metrics": {"inventory": list(INVENTORY_METRICS)},
        "agents": ["inventory"],
        "versions": {},
    }
    with pytest.raises(ValueError, match="Metric unavailable"):
        prepare_context({}, [update("metric_id", "invented")], **kwargs)
    with pytest.raises(ValueError, match="explicit"):
        prepare_context({}, [update("warehouse_scope", None)], **kwargs)


async def test_never_policy_does_not_execute_model_guesses():
    flow, coord = context_flow(
        [plan(updates=[update("warehouse_scope", "上海仓")])], policy="never"
    )
    result = await flow.run("查库存")
    assert result["error"] == "unconfirmed_context"
    coord.agents["inventory"].ainvoke.assert_not_awaited()


def test_condition_clear_is_field_specific_and_metric_version_invalidates_approval():
    kwargs = {
        "followup": True,
        "question": "上海仓",
        "metrics": {"inventory": list(INVENTORY_METRICS)},
        "agents": ["inventory"],
        "versions": {"inventory": "v1"},
    }
    initial = prepare_context(
        {},
        [
            update("metric_id", "low_stock_records"),
            update("warehouse_scope", "上海仓", "上海仓"),
        ],
        **kwargs,
    )
    confirmed = approve_context(initial.model_dump(mode="json"), "approved-v1")
    cleared = prepare_context(
        confirmed,
        [update("warehouse_scope", None, "所有仓库")],
        **{**kwargs, "question": "所有仓库"},
    )
    assert "warehouse_scope" not in cleared.conditions
    with pytest.raises(ValueError, match="explicit"):
        prepare_context(
            confirmed,
            [update("time_range", None, "所有仓库")],
            **{**kwargs, "question": "所有仓库"},
        )
    revised = [m.model_copy(update={"version": "2"}) for m in INVENTORY_METRICS]
    changed = prepare_context(
        confirmed, [], **{**kwargs, "metrics": {"inventory": revised}}
    )
    assert changed.conditions["metric_id"].status == "proposed"
    assert changed.conditions["metric_id"].approved_plan_version is None
    assert confirmed["conditions"]["metric_id"]["status"] == "approved"


async def test_inventory_conditions_do_not_leak_to_unrelated_worker_or_thread():
    flow, coord = context_flow([
        plan(updates=[update("metric_id", "low_stock_records")]),
        plan(),
    ])
    first = await flow.run("查库存不足", thread_id="alice")
    approved = await decide(flow, first, "approve")
    raw = approved["query_context"]
    other = coord._worker_context(raw, "sales")
    assert other["metric"] is None
    assert "metric_id" not in other["conditions"]
    assert raw["metric"]["id"] == "low_stock_records"
    fresh = await flow.run("查库存", thread_id="bob")
    assert fresh["confirmed_context"] == {}
    assert fresh["query_context"]["conditions"] == {}


def test_inventory_dictionary_matches_sample_sql(database):
    rows = json.loads(
        (ROOT / "scripts/data/postgres_data.json").read_text(encoding="utf-8")
    )["inventory"]
    low = [r for r in rows if r["quantity"] < r["reorder_level"]]
    expected = {
        "inventory_quantity": sum(r["quantity"] for r in rows),
        "inventory_records": len(rows),
        "low_stock_records": len(low),
        "low_stock_products": len({r["product_id"] for r in low}),
    }
    with database._engine.connect() as connection:
        for metric in INVENTORY_METRICS:
            aggregate, _, condition = metric.expression.partition(" WHERE ")
            sql = f"SELECT {aggregate} FROM inventory" + (
                f" WHERE {condition}" if condition else ""
            )  # noqa: S608 - trusted built-in metric definitions
            assert connection.exec_driver_sql(sql).scalar() == expected[metric.id]
