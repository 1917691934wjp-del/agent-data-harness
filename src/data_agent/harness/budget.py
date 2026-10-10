"""Request-local, thread-safe budgets shared by concurrent workers."""

import asyncio
from contextvars import ContextVar
import threading
from typing import Any


class BudgetExceeded(RuntimeError):  # noqa: N818 - public runtime signal
    """Execution has been stopped by a runtime policy."""


current_budget: ContextVar["Budget | None"] = ContextVar("harness_budget", default=None)


class Budget:
    def __init__(self, policy: Any) -> None:  # noqa: ANN401 - avoid import cycle
        self.policy = policy
        self.lock = threading.RLock()
        self.counts = {"llm_calls": 0, "tool_calls": 0, "retries": 0, "agent_tasks": 0}
        self.tokens = 0
        self.cost = 0.0
        self.violation: str | None = None
        self.usage_missing = False
        self.model_timeouts = 0

    def fail(self, reason: str) -> None:
        self.violation = self.violation or reason
        raise BudgetExceeded(self.violation)

    def check(self) -> None:
        with self.lock:
            if self.violation:
                raise BudgetExceeded(self.violation)

    def consume(self, kind: str, count: int = 1) -> None:
        with self.lock:
            self.check()
            if self.counts[kind] + count > getattr(self.policy, "max_" + kind):
                self.fail(f"max_{kind} exceeded")
            self.counts[kind] += count

    def usage(
        self, total: int | None, input_tokens: int | None, output_tokens: int | None
    ) -> None:
        with self.lock:
            if type(total) is not int or total < 0:
                total = None
            if type(input_tokens) is not int or input_tokens < 0:
                input_tokens = None
            if type(output_tokens) is not int or output_tokens < 0:
                output_tokens = None
            if total is None:
                self.usage_missing = True
                if (
                    self.policy.max_tokens is not None
                    or self.policy.max_cost_usd is not None
                ):
                    self.fail(
                        "Model usage unavailable for configured token/cost budget"
                    )
                return
            self.tokens += total
            if (
                self.policy.max_tokens is not None
                and self.tokens > self.policy.max_tokens
            ):
                self.fail("max_tokens exceeded after model completion")
            if self.policy.max_cost_usd is not None:
                if input_tokens is None or output_tokens is None:
                    self.fail(
                        "Input/output usage unavailable for configured cost budget"
                    )
                self.cost += (
                    input_tokens * self.policy.input_usd_per_million
                    + output_tokens * self.policy.output_usd_per_million
                ) / 1_000_000
                if self.cost > self.policy.max_cost_usd:
                    self.fail("max_cost_usd exceeded after model completion")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                **self.counts,
                "known_tokens": self.tokens,
                "usage_missing": self.usage_missing,
                "estimated_cost_usd": self.cost
                if self.policy.max_cost_usd is not None
                else None,
                "violation": self.violation,
                "model_timeouts": self.model_timeouts,
            }


async def invoke_model(model: Any, messages: list) -> Any:  # noqa: ANN401 - Runnable adapter
    budget = current_budget.get()
    if not budget:
        return await model.ainvoke(messages)
    budget.check()
    deadline = asyncio.timeout(budget.policy.model_timeout)
    try:
        async with deadline:
            return await model.ainvoke(messages)
    except TimeoutError:
        if deadline.expired():
            with budget.lock:
                budget.model_timeouts += 1
        raise
