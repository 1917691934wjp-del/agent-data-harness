"""Runtime isolation, cancellation and metadata-only tracing."""
# ruff: noqa: PLR6301, ARG002

import asyncio
from uuid import uuid4

from pydantic import ValidationError
import pytest
from test_human_review import cross_plan, review_flow

from data_agent.harness import AgentHarness, RuntimePolicy
from data_agent.harness.tracing import TraceCollector


class Flow:
    async def run(self, **kwargs):
        collector = kwargs["callbacks"][0]
        span_id = uuid4()
        collector.on_chain_start(
            None, {"secret": "private"}, run_id=span_id, name="plan_tasks"
        )
        await asyncio.sleep(0)
        collector.on_chain_end({}, run_id=span_id)
        return {"final_response": kwargs["question"]}


@pytest.mark.asyncio
async def test_concurrent_runs_are_isolated():
    harness = AgentHarness(Flow())
    first, second = await asyncio.gather(harness.run("a"), harness.run("b"))
    assert first.run_id != second.run_id
    assert first.thread_id != second.thread_id
    assert first.output["final_response"] == "a"
    assert len(first.trace) == len(second.trace) == 1
    assert first.trace[0]["span_id"] != second.trace[0]["span_id"]
    assert "private" not in first.model_dump_json()
    assert first.metrics["total_tokens"] is None


@pytest.mark.asyncio
async def test_timeout_cancels_flow():
    cleaned = asyncio.Event()

    class SlowFlow:
        async def run(self, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    result = await AgentHarness(SlowFlow(), RuntimePolicy(request_timeout=0.01)).run(
        "q"
    )
    assert result.status == "timeout"
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_external_cancellation_propagates():
    class CancelFlow:
        async def run(self, **kwargs):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await AgentHarness(CancelFlow()).run("q")


def test_policy_rejects_unimplemented_or_invalid_limits():
    for values in (
        {"unknown_limit": 1},
        {"request_timeout": 0},
        {"request_timeout": float("inf")},
    ):
        with pytest.raises(ValidationError):
            RuntimePolicy(**values)


@pytest.mark.asyncio
async def test_compiled_flow_receives_observer():
    flow, _ = review_flow([cross_plan()])
    result = await AgentHarness(flow).run("count")
    assert result.status == "paused"
    assert result.trace
    assert any(span["name"] == "review_plan" for span in result.trace)


def test_trace_parentage_limit_and_unfinished_spans():
    collector = TraceCollector(2)
    parent, child = uuid4(), uuid4()
    collector.on_chain_start(None, {}, run_id=parent, name="root")
    collector.on_chain_start(None, {}, run_id=child, parent_run_id=parent, name="child")
    collector.on_chain_start(None, {}, run_id=uuid4(), name="overflow")
    collector.on_chain_end({}, run_id=child)
    collector.close()
    assert collector.truncated
    assert len(collector.spans) == 2
    assert collector.spans[0]["parent_span_id"] == str(parent)
    assert collector.spans[1]["status"] == "unfinished"


@pytest.mark.asyncio
async def test_driver_timeout_is_not_request_timeout():
    class DriverFlow:
        async def run(self, **kwargs):
            raise TimeoutError("driver timeout")

    with pytest.raises(TimeoutError, match="driver timeout"):
        await AgentHarness(DriverFlow()).run("q")
