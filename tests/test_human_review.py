"""Offline review lifecycle tests against the compiled graph and entry points."""

import asyncio
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from a2a.types import TaskState
from langgraph.checkpoint.memory import InMemorySaver
import pytest
from test_collaboration import coordinator, flow_for, table, task, worker

from data_agent.models.collaboration import (
    CollaborationPlan,
    CollaborationSettings,
    JoinSpec,
)
from data_agent.utils.review import human_reply


def review_flow(plans, policy="cross_source"):
    coord = coordinator(
        {"sales": worker(), "stock": worker()},
        plans=plans,
        settings=CollaborationSettings(review_policy=policy),
    )
    return flow_for(coord), coord


def cross_plan():
    return CollaborationPlan(tasks=[task("sales"), task("stock", deps=["sales"])])


def payload(result):
    return result["__interrupt__"][0].value


@pytest.mark.asyncio
async def test_review_approval_runs_exact_checkpointed_plan_once():
    flow, coord = review_flow([cross_plan()])
    first = await flow.run("compare")
    thread = first["thread_id"]
    review = payload(first)
    assert review["type"] == "plan_review"
    assert review["plan"]["tasks"][1]["depends_on"] == ["sales"]
    for agent in coord.agents.values():
        agent.ainvoke.assert_not_awaited()
    done = await flow.run(thread_id=thread, resume_value=human_reply(review, "确认"))
    assert len(done["agent_results"]) == 2
    coord.planner.ainvoke.assert_awaited_once()
    for agent in coord.agents.values():
        agent.ainvoke.assert_awaited_once()
    assert await flow.get_pending_input(thread) is None
    with pytest.raises(ValueError, match="No pending"):
        await flow.run(thread_id=thread, resume_value=human_reply(review, "approve"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        {},
        {"action": "approve", "plan_version": "stale"},
        {"action": "revise", "plan_version": "stale", "feedback": ""},
        {"action": "unknown", "plan_version": "stale"},
    ],
)
async def test_invalid_or_stale_decision_stays_paused(decision):
    flow, coord = review_flow([cross_plan()])
    first = await flow.run("compare", thread_id="review")
    second = await flow.run(thread_id="review", resume_value=decision)
    assert payload(second)["plan_version"] == payload(first)["plan_version"]
    for agent in coord.agents.values():
        agent.ainvoke.assert_not_awaited()
    done = await flow.run(
        thread_id="review", resume_value=human_reply(payload(second), "approve")
    )
    assert done["error"] is None


@pytest.mark.asyncio
async def test_revision_of_cross_source_plan_requires_new_confirmation_even_if_single():
    flow, coord = review_flow([cross_plan(), CollaborationPlan(tasks=[task("stock")])])
    first = await flow.run("compare", thread_id="revise")
    old = payload(first)
    second = await flow.run(
        thread_id="revise", resume_value=human_reply(old, "只看库存")
    )
    new = payload(second)
    assert new["plan_version"] != old["plan_version"]
    assert len(new["plan"]["tasks"]) == 1
    assert "只看库存" in str(coord.planner.ainvoke.call_args)
    stale = await flow.run(thread_id="revise", resume_value=human_reply(old, "approve"))
    assert "__interrupt__" in stale
    for agent in coord.agents.values():
        agent.ainvoke.assert_not_awaited()
    done = await flow.run(thread_id="revise", resume_value=human_reply(new, "approve"))
    assert done["final_response"] == "ok"
    coord.agents["sales"].ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_and_start_new_turn_does_not_reuse_approval():
    flow, coord = review_flow([cross_plan(), cross_plan()])
    first = await flow.run("compare", thread_id="cancel")
    cancelled = await flow.run(
        thread_id="cancel", resume_value=human_reply(payload(first), "取消")
    )
    assert cancelled["error"] == "cancelled"
    assert not cancelled["agent_results"]
    assert await flow.get_pending_input("cancel") is None
    again = await flow.run("compare again", thread_id="cancel")
    assert payload(again)["plan_version"] != payload(first)["plan_version"]
    for agent in coord.agents.values():
        agent.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_question_cannot_overwrite_pending_review():
    flow, coord = review_flow([cross_plan()])
    first = await flow.run("compare", thread_id="pending")
    with pytest.raises(ValueError, match="Human input is pending"):
        await flow.run("ignore review", thread_id="pending")
    assert (await flow.get_pending_input("pending"))["plan_version"] == payload(first)[
        "plan_version"
    ]
    coord.planner.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "plan", "pause"),
    [
        ("cross_source", CollaborationPlan(tasks=[task("sales")]), False),
        ("always", CollaborationPlan(tasks=[task("sales")]), True),
        ("never", cross_plan(), False),
        (
            "cross_source",
            CollaborationPlan(
                tasks=[task("a", "sales"), task("b", "sales")],
                join=JoinSpec(task_ids=["a", "b"], keys=["product_id"]),
            ),
            True,
        ),
    ],
)
async def test_review_policies(policy, plan, pause):
    flow, _ = review_flow([plan], policy)
    result = await flow.run("query")
    assert bool(result.get("__interrupt__")) is pause


@pytest.mark.asyncio
async def test_clarification_then_review_and_cancel():
    flow, coord = review_flow([
        CollaborationPlan(clarification="Which month?"),
        cross_plan(),
    ])
    first = await flow.run("compare", thread_id="clarify")
    assert payload(first)["type"] == "clarification_needed"
    second = await flow.run(
        thread_id="clarify", resume_value={"question": "compare last month"}
    )
    assert payload(second)["type"] == "plan_review"
    await flow.run(
        thread_id="clarify", resume_value=human_reply(payload(second), "cancel")
    )
    for agent in coord.agents.values():
        agent.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_clarification():
    flow, _ = review_flow([CollaborationPlan(clarification="Which month?")])
    first = await flow.run("compare", thread_id="clarify")
    result = await flow.run(
        thread_id="clarify", resume_value=human_reply(payload(first), "cancel")
    )
    assert result["error"] == "cancelled"
    assert await flow.get_pending_input("clarify") is None


@pytest.mark.asyncio
async def test_revision_needing_clarification_still_requires_final_review():
    flow, coord = review_flow([
        cross_plan(),
        CollaborationPlan(clarification="Which warehouse?"),
        CollaborationPlan(tasks=[task("stock")]),
    ])
    first = await flow.run("compare", thread_id="clarify-revision")
    second = await flow.run(
        thread_id="clarify-revision",
        resume_value=human_reply(payload(first), "only a warehouse"),
    )
    assert payload(second)["type"] == "clarification_needed"
    third = await flow.run(
        thread_id="clarify-revision", resume_value={"question": "Beijing stock"}
    )
    assert payload(third)["type"] == "plan_review"
    for agent in coord.agents.values():
        agent.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_does_not_bypass_join_checks():
    plan = cross_plan()
    plan.join = JoinSpec(task_ids=["sales", "stock"], keys=["product_id"])
    flow, coord = review_flow([plan])
    coord.agents["sales"].ainvoke.return_value = {
        "result": table([["A", 1], ["A", 2]]),
        "generated_sql": "SELECT 1",
    }
    first = await flow.run("compare", thread_id="unsafe-join")
    result = await flow.run(
        thread_id="unsafe-join", resume_value=human_reply(payload(first), "approve")
    )
    assert result["result"] is None
    assert any("duplicate join keys" in w for w in result["collaboration_warnings"])


@pytest.mark.asyncio
async def test_approval_from_another_conversation_is_rejected():
    flow, coord = review_flow([cross_plan(), cross_plan()])
    first = await flow.run("compare", thread_id="first")
    second = await flow.run("compare", thread_id="second")
    result = await flow.run(
        thread_id="second", resume_value=human_reply(payload(first), "approve")
    )
    assert payload(result)["plan_version"] == payload(second)["plan_version"]
    for agent in coord.agents.values():
        agent.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_injected_checkpoint_preserves_pending_review_across_graph_rebuild():
    flow, coord = review_flow([cross_plan()])
    flow._checkpointer = InMemorySaver()
    flow._graph = flow._build_workflow()
    first = await flow.run("compare", thread_id="restore")
    flow._graph = flow._build_workflow()
    restored = await flow.get_pending_input("restore")
    assert restored["plan_version"] == payload(first)["plan_version"]
    await flow.run(thread_id="restore", resume_value=human_reply(restored, "approve"))
    coord.planner.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_conversation_cannot_run_two_requests_at_once():
    flow, coord = review_flow([cross_plan()])
    started = asyncio.Event()
    finish = asyncio.Event()

    async def slow_plan(*_args, **_kwargs):
        started.set()
        await finish.wait()
        return cross_plan()

    coord.planner.ainvoke.side_effect = slow_plan
    first = asyncio.create_task(flow.run("compare", thread_id="busy"))
    await started.wait()
    try:
        with pytest.raises(RuntimeError, match="already processing"):
            await flow.run("other", thread_id="busy")
    finally:
        finish.set()
        await first


@pytest.mark.asyncio
async def test_cli_handles_multiple_interrupts_until_completion(monkeypatch):
    cli = import_module("data_agent.cli.app")

    flow, coord = review_flow([cross_plan(), cross_plan()])
    replies = iter(["only last month", "approve"])
    monkeypatch.setattr(cli.Prompt, "ask", lambda *_args, **_kwargs: next(replies))
    result, _ = await cli.execute_query(flow, "compare", "cli")
    assert result["error"] is None
    assert coord.planner.ainvoke.await_count == 2


@pytest.mark.asyncio
@pytest.mark.filterwarnings(
    "ignore:Support for class-based `config` is deprecated:DeprecationWarning"
)
async def test_web_shows_review_and_resumes_displayed_version(tmp_path, monkeypatch):
    ui = import_module("data_agent.ui.app")
    from data_agent.ui.accounts import AccountStore
    from chainlit.auth import create_jwt

    accounts = AccountStore(tmp_path)
    accounts.register("reviewer", "offline-review-password", ["contoso"])
    user = accounts.verify("reviewer", "offline-review-password")
    monkeypatch.setattr(ui, "accounts", accounts)
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "offline-review-secret" * 4)
    monkeypatch.setattr(
        ui.cl,
        "context",
        SimpleNamespace(session=SimpleNamespace(token=create_jwt(user))),
    )

    flow, coord = review_flow([cross_plan()])
    session = {
        "flow": flow,
        "thread_id": "web",
        "graph_thread_id": "web",
        "user": user,
        "authorized_agents": frozenset(coord.agents),
    }
    monkeypatch.setattr(
        ui.cl,
        "user_session",
        SimpleNamespace(
            get=session.get, set=lambda key, value: session.update({key: value})
        ),
    )
    message = Mock(return_value=SimpleNamespace(send=AsyncMock()))
    monkeypatch.setattr(ui.cl, "Message", message)
    monkeypatch.setattr(ui.cl, "Step", Mock(return_value=AsyncMock()))
    await ui.on_message(SimpleNamespace(content="compare"))
    assert session["pending_input"]["type"] == "plan_review"
    await ui.on_message(SimpleNamespace(content="取消"))
    assert session["pending_input"] is None
    assert any("已取消查询" in str(call) for call in message.call_args_list)
    for agent in coord.agents.values():
        agent.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.filterwarnings(
    "ignore:'HTTP_413_REQUEST_ENTITY_TOO_LARGE' is deprecated:DeprecationWarning"
)
@pytest.mark.parametrize("action", ["cancel", "approve"])
async def test_a2a_review_metadata_resume_and_cancel(monkeypatch, action):
    module = import_module("data_agent.a2a.executor")

    flow, coord = review_flow([cross_plan()])
    executor = module.DataAgentExecutor.__new__(module.DataAgentExecutor)
    executor.agent = flow
    executor._connected = True
    updater = SimpleNamespace(
        update_status=AsyncMock(),
        add_artifact=AsyncMock(),
        complete=AsyncMock(),
        cancel=AsyncMock(),
    )
    monkeypatch.setattr(module, "TaskUpdater", Mock(return_value=updater))
    task_record = SimpleNamespace(id="task", context_id="a2a")
    context = SimpleNamespace(
        current_task=task_record,
        message=SimpleNamespace(metadata={}),
        get_user_input=lambda: "compare",
    )
    await executor.execute(context, AsyncMock())
    args = updater.update_status.call_args.args
    assert args[0] == TaskState.input_required
    review = args[1].metadata["human_input"]
    updater.complete.assert_not_awaited()
    # Plain text is not sufficient for versioned API approval.
    context.get_user_input = lambda: "approve"
    await executor.execute(context, AsyncMock())
    for agent in coord.agents.values():
        agent.ainvoke.assert_not_awaited()
    context.message.metadata = {"review_decision": human_reply(review, action)}
    await executor.execute(context, AsyncMock())
    if action == "cancel":
        assert updater.update_status.call_args.args[0] == TaskState.canceled
        updater.complete.assert_not_awaited()
    else:
        updater.complete.assert_awaited_once()
        updater.add_artifact.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.filterwarnings(
    "ignore:'HTTP_413_REQUEST_ENTITY_TOO_LARGE' is deprecated:DeprecationWarning"
)
async def test_a2a_cancel_paused_task(monkeypatch):
    module = import_module("data_agent.a2a.executor")

    flow, coord = review_flow([cross_plan()])
    await flow.run("compare", thread_id="a2a")
    executor = module.DataAgentExecutor.__new__(module.DataAgentExecutor)
    executor.agent = flow
    updater = SimpleNamespace(cancel=AsyncMock())
    monkeypatch.setattr(module, "TaskUpdater", Mock(return_value=updater))
    await executor.cancel(
        SimpleNamespace(current_task=SimpleNamespace(id="t", context_id="a2a")),
        AsyncMock(),
    )
    updater.cancel.assert_awaited_once()
    for agent in coord.agents.values():
        agent.ainvoke.assert_not_awaited()
