"""Validated task plans and per-task evidence for collaborating data agents."""

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from data_agent.context import ContextUpdate
from data_agent.models.outputs import QueryResult


class AgentTask(BaseModel):
    """A bounded query delegated to one registered agent."""

    task_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    agent_name: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=4000)
    depends_on: list[str] = Field(default_factory=list, max_length=8)


class JoinSpec(BaseModel):
    """Optional one-to-one full outer join using common output column aliases."""

    task_ids: list[str] = Field(min_length=2, max_length=8)
    keys: list[str] = Field(min_length=1, max_length=4)


class CollaborationPlan(BaseModel):
    """A small acyclic task graph, or a request for user clarification."""

    tasks: list[AgentTask] = Field(default_factory=list, max_length=8)
    clarification: str | None = None
    join: JoinSpec | None = None
    context_mode: Literal["new", "followup"] = "new"
    context_updates: list[ContextUpdate] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        """Reject duplicate IDs, dangling dependencies and cycles before execution."""
        ids = [task.task_id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("Task IDs must be unique")
        if self.tasks and self.clarification:
            raise ValueError("Clarify before scheduling tasks")
        if not self.tasks and self.join:
            raise ValueError("A join requires tasks")
        self._validate_dependencies(set(ids))
        self._validate_join(set(ids))
        return self

    def _validate_dependencies(self, ids: set[str]) -> None:
        completed: set[str] = set()
        pending = list(self.tasks)
        for task in pending:
            if len(task.depends_on) != len(set(task.depends_on)):
                raise ValueError("Dependencies must be unique")
            if not set(task.depends_on) <= ids:
                raise ValueError("Unknown dependency")
        while pending:
            ready = [t for t in pending if set(t.depends_on) <= completed]
            if not ready:
                raise ValueError("Task dependencies must be acyclic")
            completed.update(t.task_id for t in ready)
            pending = [t for t in pending if t.task_id not in completed]

    def _validate_join(self, ids: set[str]) -> None:
        if self.join:
            if not set(self.join.task_ids) <= ids:
                raise ValueError("Unknown join task")
            if len(set(self.join.task_ids)) != len(self.join.task_ids):
                raise ValueError("Join tasks must be unique")
            if len(set(self.join.keys)) != len(self.join.keys):
                raise ValueError("Join keys must be unique")


class AgentTaskResult(BaseModel):
    """Evidence exchanged between agents, retaining task and source provenance."""

    task_id: str
    agent_name: str
    question: str
    status: Literal["success", "error", "timeout", "blocked"]
    depends_on: list[str] = Field(default_factory=list)
    generated_sql: str = ""
    result: QueryResult | None = None
    final_response: str = ""
    error: str | None = None
    visualization_image: str | None = None
    visualization_code: str | None = None
    visualization_error: str | None = None


class CollaborationSettings(BaseModel):
    """Execution and prompt bounds independent of model-generated plans."""

    max_concurrency: int = Field(default=4, ge=1, le=8)
    task_timeout: float = Field(default=120, gt=0)
    model_timeout: float = Field(default=60, gt=0)
    max_context_chars: int = Field(default=30000, ge=1000)
    max_join_rows: int = Field(default=10000, ge=1)
    review_policy: Literal["cross_source", "always", "never"] = "cross_source"


class ReviewDecision(BaseModel):
    """A decision applies only to the checkpointed version of a plan."""

    action: Literal["approve", "revise", "cancel"]
    plan_version: str
    feedback: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def require_feedback(self) -> Self:
        if self.action == "revise" and not (self.feedback or "").strip():
            raise ValueError("Revision requires feedback")
        return self
