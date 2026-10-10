"""Unified runtime entry point; flow lifecycle remains owned by the caller."""

import asyncio
import os
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from data_agent.harness.budget import Budget, BudgetExceeded, current_budget
from data_agent.harness.store import save_trace
from data_agent.harness.tracing import TraceCollector

if TYPE_CHECKING:
    from data_agent.agent import DataAgentFlow


class RuntimePolicy(BaseModel):
    """Only expose limits that this implementation actually enforces."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    request_timeout: float = Field(default=60, gt=0, allow_inf_nan=False)
    max_trace_spans: int = Field(default=1000, ge=1, le=10000)
    max_llm_calls: int = Field(default=20, ge=1)
    max_tool_calls: int = Field(default=20, ge=0)
    max_retries: int = Field(default=6, ge=0)
    max_agent_tasks: int = Field(default=8, ge=1, le=8)
    max_concurrency: int = Field(default=4, ge=1, le=8)
    task_timeout: float = Field(default=30, gt=0, allow_inf_nan=False)
    model_timeout: float = Field(default=30, gt=0, allow_inf_nan=False)
    max_tokens: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    input_usd_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    output_usd_per_million: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    allowed_agents: frozenset[str] | None = None
    allow_local_code: bool = False
    max_question_chars: int = Field(default=12000, ge=1)
    max_result_rows: int = Field(default=10000, ge=1)
    forbidden_sql_columns: frozenset[str] = frozenset()
    redact_columns: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def cost_requires_prices(self) -> "RuntimePolicy":
        if self.max_cost_usd is not None and (
            self.input_usd_per_million is None or self.output_usd_per_million is None
        ):
            raise ValueError("Cost budget requires explicit input and output prices")
        return self

    @classmethod
    def from_environment(cls) -> "RuntimePolicy":
        path = os.getenv("DATA_AGENT_HARNESS_POLICY")
        return (
            cls.model_validate_json(Path(path).read_text(encoding="utf-8-sig"))
            if path
            else cls()
        )

    def report_config(self) -> dict[str, Any]:
        config = self.model_dump(mode="json")
        for key in ("allowed_agents", "forbidden_sql_columns", "redact_columns"):
            if config[key] is not None:
                config[key] = sorted(config[key])
        return config


class HarnessResult(BaseModel):
    run_id: str
    thread_id: str
    status: str
    output: dict[str, Any] | None = None
    error_type: str | None = None
    elapsed_ms: float
    metrics: dict[str, Any]
    trace: list[dict[str, Any]]
    trace_truncated: bool
    budget: dict[str, Any] = Field(default_factory=dict)
    trace_export_error: str | None = None
    policy: dict[str, Any] = Field(default_factory=dict)


class AgentHarness:
    """Wrap each turn with isolated observers, timeout and a result envelope."""

    def __init__(
        self, flow: "DataAgentFlow", policy: RuntimePolicy | None = None
    ) -> None:
        self.flow = flow
        self.policy = policy or RuntimePolicy.from_environment()

    async def run(  # noqa: C901, PLR0912, PLR0915 - runtime outcome boundary
        self,
        question: str | None = None,
        thread_id: str | None = None,
        resume_value: dict[str, Any] | None = None,
    ) -> HarnessResult:
        if (question is None) == (resume_value is None):
            raise ValueError("Provide exactly one question or resume decision")
        if question is not None and not question.strip():
            raise ValueError("Question must not be empty")
        if question is not None and len(question) > self.policy.max_question_chars:
            raise ValueError("Question exceeds max_question_chars")
        if resume_value is not None and not thread_id:
            raise ValueError("Resuming requires thread_id")
        if resume_value is not None:
            for key in ("question", "feedback"):
                value = resume_value.get(key)
                if value is not None and (
                    not isinstance(value, str)
                    or len(value) > self.policy.max_question_chars
                ):
                    raise ValueError("Resume text exceeds input policy")
        thread_id = thread_id or uuid4().hex
        run_id = uuid4().hex
        budget = Budget(self.policy)
        collector = TraceCollector(self.policy.max_trace_spans, budget)
        token = current_budget.set(budget)
        started = time.perf_counter()
        output = None
        error_type = None
        status = "cancelled"
        export_error = None
        deadline = asyncio.timeout(self.policy.request_timeout)
        try:
            async with deadline:
                output = await self.flow.run(
                    question=question,
                    thread_id=thread_id,
                    resume_value=resume_value,
                    callbacks=[collector],
                )
            budget.check()
            if "__interrupt__" in output:
                status = "paused"
            elif output.get("error") == "cancelled":
                status = "cancelled"
            elif output.get("error"):
                status = "error"
            elif output.get("collaboration_warnings"):
                status = "partial"
            else:
                status = "completed"
        except TimeoutError:
            if not deadline.expired():
                status, error_type = "failed", "TimeoutError"
                raise
            status, error_type = "timeout", "TimeoutError"
        except BudgetExceeded:
            status, error_type = "budget_exceeded", "BudgetExceeded"
        except Exception as exc:
            status, error_type = "failed", type(exc).__name__
            raise
        # Cancellation and programming/validation errors propagate to the caller.
        finally:
            collector.close()
            current_budget.reset(token)
            directory = os.getenv("DATA_AGENT_HARNESS_TRACE_DIR")
            if directory:
                try:
                    save_trace(
                        directory,
                        {
                            "schema_version": 1,
                            "run_id": run_id,
                            "thread_id": thread_id,
                            "status": status,
                            "error_type": error_type,
                            "elapsed_ms": (time.perf_counter() - started) * 1000,
                            "metrics": collector.snapshot(),
                            "trace": collector.spans,
                            "trace_truncated": collector.truncated,
                            "budget": budget.snapshot(),
                            "policy": self.policy.report_config(),
                        },
                    )
                except OSError as exc:
                    export_error = type(exc).__name__
        snapshot = collector.snapshot()
        return HarnessResult(
            run_id=run_id,
            thread_id=thread_id,
            status=status,
            output=output,
            error_type=error_type,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            metrics={key: value for key, value in snapshot.items() if key != "events"},
            trace=collector.spans,
            trace_truncated=collector.truncated,
            budget=budget.snapshot(),
            trace_export_error=export_error,
            policy=self.policy.report_config(),
        )

    async def run_output(self, *args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        """Compatibility boundary for existing result consumers."""
        result = await self.run(*args, **kwargs)
        if result.status == "timeout":
            raise TimeoutError("Harness request timeout")
        if result.status == "budget_exceeded":
            raise BudgetExceeded(result.budget["violation"])
        output = dict(result.output or {})
        output["harness"] = result.model_dump(exclude={"output"})
        return output
