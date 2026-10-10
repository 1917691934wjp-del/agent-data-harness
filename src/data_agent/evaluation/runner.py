"""Sequential case execution with isolated conversations and explicit HITL scripts."""

import asyncio
from collections.abc import Callable
import time
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel

from data_agent.evaluation.grading import grade
from data_agent.evaluation.models import (
    CaseResult,
    EvalCase,
    EvalStep,
    EvalSuite,
    Observation,
    StepResult,
)
from data_agent.harness import AgentHarness
from data_agent.harness.judge import AnswerJudge


class CaseSession(Protocol):
    async def execute(self, step: EvalStep) -> Observation: ...
    async def close(self) -> None: ...
    def metrics(self) -> dict[str, Any]: ...


def _dump(value: object) -> object:
    return value.model_dump() if isinstance(value, BaseModel) else value


def observe(output: dict[str, Any]) -> Observation:
    interrupts = output.get("__interrupt__")
    pending = interrupts[0].value if interrupts else None
    tasks = [_dump(task) for task in output.get("agent_results", [])]
    plan = _dump(output.get("collaboration_plan"))
    if pending:
        plan = pending.get("plan") or plan
    agents = sorted({t["agent_name"] for t in tasks})
    if not agents and plan:
        agents = sorted({t["agent_name"] for t in plan.get("tasks", [])})
    if not agents and output.get("datasource_name"):
        agents = [
            part.strip()
            for part in output["datasource_name"].split(",")
            if part.strip()
        ]
    error = output.get("error")
    warnings = output.get("collaboration_warnings", [])
    if pending:
        status = pending["type"]
    elif error == "cancelled":
        status = "cancelled"
    elif error:
        status = "error"
    elif warnings or any(t["status"] != "success" for t in tasks):
        status = "partial"
    else:
        status = "success"
    return Observation(
        status=status,
        agents=agents,
        error=error,
        table=_dump(output.get("result")) or None,
        task_statuses={t["task_id"]: t["status"] for t in tasks},
        warnings=warnings,
        answer=output.get("final_response", ""),
        sql=output.get("generated_sql", ""),
        pending=pending,
        query_context=output.get("query_context", {}),
        confirmed_context=output.get("confirmed_context", {}),
        task_dependencies={
            t["task_id"]: t.get("depends_on", []) for t in (plan or {}).get("tasks", [])
        },
        join_keys=((plan or {}).get("join") or {}).get("keys", []),
        evidence=[
            {
                key: task.get(key)
                for key in ("task_id", "agent_name", "status", "result", "error")
            }
            for task in tasks
        ],
    )


class LiveSession:
    """One flow per case; only authored steps can resume human input."""

    def __init__(self, flow: Any, collector: Any) -> None:  # noqa: ANN401 - injectable flow and callback adapters
        self.flow = flow
        self.collector = collector
        self.thread_id = "eval-" + uuid4().hex
        self.pending: dict[str, Any] | None = None
        self.connected = False
        self.harness = AgentHarness(flow)
        self.harness_runs: list[dict[str, Any]] = []

    async def execute(self, step: EvalStep) -> Observation:
        if not self.connected:
            await self.flow.connect()
            self.connected = True
        if step.resume is not None:
            if not self.pending:
                raise ValueError("Script attempted to resume without pending input")
            if (
                step.resume.question is not None
                and self.pending["type"] != "clarification_needed"
            ):
                raise ValueError("Clarification reply cannot approve a plan")
            if (
                step.resume.action in {"approve", "revise"}
                and self.pending["type"] != "plan_review"
            ):
                raise ValueError("Plan decision cannot resume a clarification")
            reply = step.resume.model_dump(exclude_none=True)
            if self.pending["type"] == "plan_review":
                reply["plan_version"] = self.pending["plan_version"]
            output = await self.harness.run_output(
                thread_id=self.thread_id, resume_value=reply
            )
        else:
            output = await self.harness.run_output(
                step.question, thread_id=self.thread_id
            )
        self.harness_runs.append(output["harness"])
        result = observe(output)
        self.pending = result.pending
        return result

    def metrics(self) -> dict[str, Any]:
        return {**self.collector.snapshot(), "harness_runs": self.harness_runs}

    async def close(self) -> None:
        await self.flow.disconnect()
        for source in self.flow.datasources.values():
            engine = getattr(source, "_engine", None)
            if engine is not None:
                await asyncio.to_thread(engine.dispose)


class ReplaySession:
    """Re-grade recorded outputs; this never measures model quality or latency."""

    def __init__(self, records: list[Observation]) -> None:
        self.records = iter(records)

    async def execute(self, step: EvalStep) -> Observation:
        del step
        try:
            return next(self.records)
        except StopIteration as exc:
            raise ValueError("Replay has no observation for this step") from exc

    async def close(self) -> None:
        self.records = iter(())

    @staticmethod
    def metrics() -> dict[str, Any]:
        return {}


async def run_case(
    case: EvalCase,
    factory: Callable[[EvalCase], CaseSession],
    timeout_seconds: float,
    judge: AnswerJudge | None = None,
) -> CaseResult:
    started = time.perf_counter()
    session = None
    results = []
    metrics = {}
    current_question = case.steps[0].question or ""
    try:  # noqa: PLR1702 - execution and cleanup each have an independent deadline
        session = factory(case)
        async with asyncio.timeout(timeout_seconds):
            for index, step in enumerate(case.steps, 1):
                current_question = (
                    step.question
                    or (step.resume.question if step.resume else None)
                    or current_question
                )
                tick = time.perf_counter()
                actual = await session.execute(step)
                failures = grade(step.expect, actual)
                judged = None
                if step.expect.judge is not None:
                    if judge is None:
                        raise ValueError(
                            "Case requires an explicitly configured answer judge"
                        )
                    judged, judge_failures = await judge.evaluate(
                        current_question, actual.model_dump(), step.expect.judge
                    )
                    failures.extend(judge_failures)
                results.append(
                    StepResult(
                        step=index,
                        request=step,
                        observation=actual,
                        failures=failures,
                        passed=not failures,
                        elapsed_ms=(time.perf_counter() - tick) * 1000,
                        judge_result=judged,
                    )
                )
    except Exception as exc:  # noqa: BLE001 - record a failed case and continue the suite
        # Infrastructure errors never pass merely because a case expected an agent error.
        failure = "runner: " + (str(exc) or type(exc).__name__)
        if len(results) == len(case.steps):
            results[-1].passed = False
            results[-1].failures.append(failure)
            results[-1].infrastructure_error = type(exc).__name__
        else:
            index = len(results)
            results.append(
                StepResult(
                    step=index + 1,
                    request=case.steps[index],
                    passed=False,
                    failures=[failure],
                    infrastructure_error=type(exc).__name__,
                    elapsed_ms=0,
                )
            )
    finally:
        if session is not None:
            try:
                metrics = session.metrics()
            except Exception as exc:  # noqa: BLE001 - isolate callback failures
                _record_cleanup_error(results, "metrics", exc)
            try:
                async with asyncio.timeout(min(timeout_seconds, 10)):
                    await session.close()
            except Exception as exc:  # noqa: BLE001 - isolate cleanup failures
                _record_cleanup_error(results, "cleanup", exc)
    return CaseResult(
        id=case.id,
        category=case.category,
        steps=results,
        passed=len(results) == len(case.steps) and all(r.passed for r in results),
        elapsed_ms=(time.perf_counter() - started) * 1000,
        **metrics,
    )


def _record_cleanup_error(
    results: list[StepResult], stage: str, exc: Exception
) -> None:
    if results:
        results[-1].passed = False
        results[-1].failures.append(f"{stage}: {type(exc).__name__}: {exc}")
        results[-1].infrastructure_error = type(exc).__name__


async def run_suite(
    suite: EvalSuite,
    factory: Callable[[EvalCase], CaseSession],
    timeout_seconds: float = 120,
    judge: AnswerJudge | None = None,
) -> list[CaseResult]:
    """Sequential runs avoid flooding services; an individual failure does not stop the suite."""
    return [
        await run_case(case, factory, timeout_seconds, judge) for case in suite.cases
    ]
