"""Strict, versioned evaluation inputs and portable run artifacts."""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from data_agent.harness.judge import (
    JudgeCriteria,  # noqa: TC001 - Pydantic runtime schema
)

Status = Literal[
    "success", "partial", "error", "clarification_needed", "plan_review", "cancelled"
]


class StrictModel(BaseModel):
    """Reject misspelled assertions instead of silently skipping checks."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ExpectedTable(StrictModel):
    """Small golden result, compared by column names and row multiplicity."""

    columns: list[str] = Field(min_length=1)
    rows: list[list[JsonValue]] = Field(max_length=200)
    ordered: bool = False
    abs_tol: float = Field(default=0.000001, ge=0)
    rel_tol: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if len(self.columns) != len(set(self.columns)):
            raise ValueError("Expected column names must be unique")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("Expected rows must match the columns")
        return self


class Expectations(StrictModel):
    status: Status
    agents: list[str] | None = None
    table: ExpectedTable | None = None
    agent_tables: dict[str, ExpectedTable] = Field(default_factory=dict)
    task_statuses: dict[str, str] = Field(default_factory=dict)
    error_contains: str | None = None
    warning_contains: list[str] = Field(default_factory=list)
    answer_contains: list[str] = Field(default_factory=list)
    judge: "JudgeCriteria | None" = None
    task_dependencies: dict[str, list[str]] | None = None
    join_keys: list[str] | None = None


class ResumeInput(StrictModel):
    action: Literal["approve", "revise", "cancel"] | None = None
    question: str | None = Field(default=None, min_length=1)
    feedback: str | None = Field(default=None, min_length=1, max_length=4000)

    @model_validator(mode="after")
    def validate_reply(self) -> Self:
        if (self.action is None) == (self.question is None):
            raise ValueError("Resume needs either a question or an action")
        if self.question is not None and not self.question.strip():
            raise ValueError("Clarification must not be blank")
        if self.action == "revise" and not (self.feedback or "").strip():
            raise ValueError("Revision requires feedback")
        if self.feedback is not None and self.action != "revise":
            raise ValueError("Only revision accepts feedback")
        return self


class EvalStep(StrictModel):
    question: str | None = Field(default=None, min_length=1)
    resume: ResumeInput | None = None
    expect: Expectations

    @model_validator(mode="after")
    def validate_input(self) -> Self:
        if (self.question is None) == (self.resume is None):
            raise ValueError("Each step needs exactly one question or resume")
        if self.question is not None and not self.question.strip():
            raise ValueError("Question must not be blank")
        return self


class EvalCase(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
    category: str = Field(min_length=1)
    description: str = ""
    steps: list[EvalStep] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def first_step_is_question(self) -> Self:
        if self.steps[0].question is None:
            raise ValueError("A case must start with a question")
        return self


class EvalSuite(StrictModel):
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1)
    description: str = ""
    data_version: str = Field(min_length=1)
    data_files_sha256: dict[str, str] = Field(default_factory=dict)
    cases: list[EvalCase] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Case IDs must be unique")
        return self


class Observation(StrictModel):
    status: Status
    agents: list[str] = Field(default_factory=list)
    table: dict[str, Any] | None = None
    task_statuses: dict[str, str] = Field(default_factory=dict)
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    answer: str = ""
    sql: str = ""
    pending: dict[str, Any] | None = None
    query_context: dict[str, Any] = Field(default_factory=dict)
    confirmed_context: dict[str, Any] = Field(default_factory=dict)
    task_dependencies: dict[str, list[str]] = Field(default_factory=dict)
    join_keys: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class StepResult(StrictModel):
    step: int
    request: EvalStep
    passed: bool
    failures: list[str]
    observation: Observation | None = None
    elapsed_ms: float
    infrastructure_error: str | None = None
    judge_result: dict[str, Any] | None = None


class CaseResult(StrictModel):
    id: str
    category: str
    passed: bool
    elapsed_ms: float
    steps: list[StepResult]
    llm_calls: int | None = None
    query_steps: int | None = None
    total_tokens: int | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    harness_runs: list[dict[str, Any]] = Field(default_factory=list)
