"""Bounded callback spans containing metadata, never prompts or results."""

import time
from typing import Any
from uuid import UUID

from langchain_core.outputs import LLMResult

from data_agent.evaluation.metrics import EvalMetrics
from data_agent.harness.budget import Budget


class TraceCollector(EvalMetrics):
    raise_error = True
    run_inline = True

    def __init__(self, limit: int, budget: Budget | None = None) -> None:
        super().__init__()
        self.limit = limit
        self.spans: list[dict[str, Any]] = []
        self.truncated = False
        self._active: dict[UUID, dict[str, Any]] = {}
        self.budget = budget
        self._usage_recorded: set[UUID] = set()

    def _start_model(self, run_id: UUID, kwargs: dict) -> None:
        with self._lock:
            seen = run_id in self._models
        if self.budget and not seen:
            self.budget.consume("llm_calls")
        self.on_chain_start(
            None,
            None,
            run_id=run_id,
            name="model",
            parent_run_id=kwargs.get("parent_run_id"),
        )
        metadata = kwargs.get("metadata") or {}
        if isinstance(metadata, dict):
            with self._lock:
                if run_id in self._active:
                    self._active[run_id]["model"] = metadata.get("ls_model_name")

    def on_chat_model_start(
        self, serialized: dict, messages: list, *, run_id: UUID, **kwargs: object
    ) -> None:
        self._start_model(run_id, kwargs)
        super().on_chat_model_start(serialized, messages, run_id=run_id, **kwargs)

    def on_llm_start(
        self, serialized: dict, prompts: list, *, run_id: UUID, **kwargs: object
    ) -> None:
        self._start_model(run_id, kwargs)
        super().on_llm_start(serialized, prompts, run_id=run_id, **kwargs)

    def on_llm_end(
        self, response: LLMResult, *, run_id: UUID, **kwargs: object
    ) -> None:
        usage = (response.llm_output or {}).get("token_usage") or {}
        if response.generations and response.generations[0]:
            message = getattr(response.generations[0][0], "message", None)
            usage = getattr(message, "usage_metadata", None) or usage
        with self._lock:
            span = self._active.get(run_id)
            if span is not None:
                span["total_tokens"] = usage.get("total_tokens")
                span["input_tokens"] = usage.get(
                    "input_tokens", usage.get("prompt_tokens")
                )
                span["output_tokens"] = usage.get(
                    "output_tokens", usage.get("completion_tokens")
                )
        super().on_llm_end(response, run_id=run_id, **kwargs)
        self._finish(run_id, "ended")
        if self.budget:
            with self._lock:
                if run_id in self._usage_recorded:
                    return
                self._usage_recorded.add(run_id)
            self.budget.usage(
                usage.get("total_tokens"),
                usage.get("input_tokens", usage.get("prompt_tokens")),
                usage.get("output_tokens", usage.get("completion_tokens")),
            )

    def on_llm_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: object
    ) -> None:
        del error, kwargs
        self._finish(run_id, "error")

    def on_chain_start(
        self,
        serialized: dict | None,
        inputs: object,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: object,
    ) -> None:
        if self.budget:
            self.budget.check()
            name = kwargs.get("name")
            if name == "retry_sql":
                self.budget.consume("retries")
        super().on_chain_start(serialized, inputs, run_id=run_id, **kwargs)
        with self._lock:
            if len(self.spans) + len(self._active) >= self.limit:
                self.truncated = True
                return
            self._active[run_id] = {
                "span_id": str(run_id),
                "parent_span_id": str(parent_run_id) if parent_run_id else None,
                "name": str(kwargs.get("name", "chain")),
                "started_at": time.time(),
                "tick": time.perf_counter(),
            }

    def _finish(self, run_id: UUID, status: str) -> None:
        super()._finish(run_id, status)
        with self._lock:
            span = self._active.pop(run_id, None)
            if span is not None:
                span["elapsed_ms"] = (time.perf_counter() - span.pop("tick")) * 1000
                span["status"] = status
                self.spans.append(span)

    def close(self) -> None:
        for run_id in list(self._active):
            self._finish(run_id, "unfinished")
