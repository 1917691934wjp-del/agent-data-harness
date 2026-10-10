"""Deterministic quality gates for comparable evaluation reports."""

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Metric = Literal[
    "pass_rate",
    "routing_accuracy",
    "result_accuracy",
    "p95_ms",
    "avg_llm_calls",
    "avg_tokens",
    "timeout_rate",
    "planning_accuracy",
    "answer_correctness",
    "answer_groundedness",
    "answer_completeness",
]


class Threshold(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    min: float | None = None
    max: float | None = None
    max_drop: float | None = Field(default=None, ge=0)
    max_increase: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def consistent_threshold(self) -> "Threshold":
        if all(
            value is None
            for value in (self.min, self.max, self.max_drop, self.max_increase)
        ):
            raise ValueError("Threshold must specify at least one constraint")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("Minimum exceeds maximum")
        return self


class QualityGate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metrics: dict[Metric, Threshold] = Field(min_length=1)


def assert_comparable(old: dict, new: dict) -> None:
    for key in ("schema_version", "suite", "data_version", "mode"):
        if old[key] != new[key]:
            raise ValueError(f"Reports differ in {key}; cannot compare")
    for key in (
        "dataset_sha256",
        "sample_files_sha256",
        "review_policy",
        "config",
        "harness_policy",
        "judge",
        "timeout_seconds",
    ):
        if old["metadata"].get(key) != new["metadata"].get(key):
            raise ValueError(f"Reports differ in {key}; cannot compare")
    ids = [[case["id"] for case in report["cases"]] for report in (old, new)]
    if any(len(values) != len(set(values)) for values in ids) or set(ids[0]) != set(
        ids[1]
    ):
        raise ValueError("Report case IDs differ or contain duplicates")


def metrics(report: dict) -> dict[str, float | None]:  # noqa: C901 - fail-closed report checks
    if report.get("schema_version") != 1 or report.get("mode") not in {
        "live",
        "replay",
    }:
        raise ValueError("Unsupported evaluation report")
    cases = report["cases"]
    if not cases:
        raise ValueError("Empty report cannot satisfy a quality gate")
    steps = [step for case in cases for step in case["steps"]]
    if not steps or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Report requires unique cases and nonempty steps")
    for case in cases:
        if not case["steps"] or type(case["passed"]) is not bool:
            raise ValueError("Invalid case result")
        if case["passed"] != all(step["passed"] for step in case["steps"]):
            raise ValueError("Case pass flag disagrees with step results")
        if (
            not isinstance(case["elapsed_ms"], (int, float))
            or not math.isfinite(case["elapsed_ms"])
            or case["elapsed_ms"] < 0
        ):
            raise ValueError("Invalid case latency")
        for key in ("llm_calls", "total_tokens"):
            value = case.get(key)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"Invalid {key}")

    def accuracy(assertion: str, label: str) -> float | None:
        selected = [
            s
            for s in steps
            if s["request"]["expect"].get(assertion) is not None
            or (assertion == "table" and s["request"]["expect"].get("agent_tables"))
        ]
        if not selected:
            return None
        return sum(
            not s.get("infrastructure_error")
            and bool(s.get("observation"))
            and not any(f.startswith(label + ":") for f in s["failures"])
            for s in selected
        ) / len(selected)

    def average(key: str) -> float | None:
        values = [c.get(key) for c in cases]
        return (
            sum(values) / len(values)
            if report["mode"] == "live" and all(v is not None for v in values)
            else None
        )

    durations = sorted(c["elapsed_ms"] for c in cases)
    judge_steps = [s for s in steps if s["request"]["expect"].get("judge") is not None]
    judged = [s["judge_result"]["scores"] for s in judge_steps if s.get("judge_result")]
    judge_complete = bool(judged) and len(judged) == len(judge_steps)
    planned = [
        s
        for s in steps
        if s["request"]["expect"].get("task_dependencies") is not None
        or s["request"]["expect"].get("join_keys") is not None
    ]
    return {
        "pass_rate": sum(c["passed"] for c in cases) / len(cases),
        "routing_accuracy": accuracy("agents", "routing"),
        "result_accuracy": accuracy("table", "table"),
        "planning_accuracy": sum(
            not s.get("infrastructure_error")
            and bool(s.get("observation"))
            and not any(f.startswith("planning:") for f in s["failures"])
            for s in planned
        )
        / len(planned)
        if planned
        else None,
        **{
            "answer_" + key: sum(s[key] for s in judged) / len(judged)
            if judge_complete
            else None
            for key in ("correctness", "groundedness", "completeness")
        },
        "p95_ms": durations[math.ceil(len(durations) * 0.95) - 1]
        if report["mode"] == "live"
        else None,
        "avg_llm_calls": average("llm_calls"),
        "avg_tokens": average("total_tokens"),
        "timeout_rate": sum(
            any(
                s.get("infrastructure_error") == "TimeoutError"
                or "timeout"
                in (s.get("observation") or {}).get("task_statuses", {}).values()
                for s in c["steps"]
            )
            or any(
                run.get("budget", {}).get("model_timeouts", 0) > 0
                for run in c.get("harness_runs", [])
            )
            for c in cases
        )
        / len(cases)
        if report["mode"] == "live"
        else None,
    }


def evaluate_gate(
    gate: QualityGate, candidate: dict, baseline: dict | None = None
) -> dict[str, Any]:
    if baseline is not None:
        assert_comparable(baseline, candidate)
    actual = metrics(candidate)
    previous = metrics(baseline) if baseline is not None else {}
    failures = []
    for name, rule in gate.metrics.items():
        value = actual[name]
        if value is None or not math.isfinite(value):
            failures.append(f"{name}: metric unavailable")
            continue
        if rule.min is not None and value < rule.min:
            failures.append(f"{name}: {value} < minimum {rule.min}")
        if rule.max is not None and value > rule.max:
            failures.append(f"{name}: {value} > maximum {rule.max}")
        if rule.max_drop is not None or rule.max_increase is not None:
            before = previous.get(name)
            if before is None:
                failures.append(f"{name}: baseline metric required")
            elif rule.max_drop is not None and before - value > rule.max_drop:
                failures.append(
                    f"{name}: drop {before - value} exceeds {rule.max_drop}"
                )
            elif rule.max_increase is not None and value - before > rule.max_increase:
                failures.append(
                    f"{name}: increase {value - before} exceeds {rule.max_increase}"
                )
    return {
        "passed": not failures,
        "failures": failures,
        "candidate": actual,
        "baseline": previous,
        "delta": {
            k: actual[k] - previous[k]
            for k in actual
            if actual[k] is not None and previous.get(k) is not None
        },
    }
