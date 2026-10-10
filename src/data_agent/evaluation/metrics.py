"""Collect metadata only; never retain prompts or model reasoning."""

import threading
import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

MAX_EVENTS = 1000


class EvalMetrics(BaseCallbackHandler):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._models: set[UUID] = set()
        self._tokens: dict[UUID, int] = {}
        self._chains: dict[UUID, tuple[str, float]] = {}
        self._queries = 0
        self._events: list[dict[str, Any]] = []

    def on_chat_model_start(
        self, serialized: dict, messages: list, *, run_id: UUID, **kwargs: object
    ) -> None:
        del serialized, messages, kwargs
        with self._lock:
            self._models.add(run_id)

    def on_llm_start(
        self, serialized: dict, prompts: list, *, run_id: UUID, **kwargs: object
    ) -> None:
        del serialized, prompts, kwargs
        with self._lock:
            self._models.add(run_id)

    def on_llm_end(
        self, response: LLMResult, *, run_id: UUID, **kwargs: object
    ) -> None:
        del kwargs
        usage = (response.llm_output or {}).get("token_usage", {})
        total = usage.get("total_tokens")
        if total is None and response.generations and response.generations[0]:
            message = getattr(response.generations[0][0], "message", None)
            total = (getattr(message, "usage_metadata", None) or {}).get("total_tokens")
        if isinstance(total, int) and total >= 0:
            with self._lock:
                self._tokens[run_id] = total

    def on_chain_start(
        self, serialized: dict | None, inputs: object, *, run_id: UUID, **kwargs: object
    ) -> None:
        del serialized, inputs
        name = str(kwargs.get("name", "chain"))
        with self._lock:
            self._chains[run_id] = (name, time.perf_counter())
            if name == "execute_query":
                self._queries += 1

    def _finish(self, run_id: UUID, status: str) -> None:
        with self._lock:
            chain = self._chains.pop(run_id, None)
            if chain and len(self._events) < MAX_EVENTS:
                name, started = chain
                self._events.append({
                    "name": name,
                    "status": status,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                })

    def on_chain_end(self, outputs: object, *, run_id: UUID, **kwargs: object) -> None:
        del outputs, kwargs
        self._finish(run_id, "ended")

    def on_chain_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: object
    ) -> None:
        del kwargs
        # LangGraph interrupt is a pause, not a model execution failure.
        self._finish(
            run_id, "paused" if type(error).__name__ == "GraphInterrupt" else "error"
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            complete = bool(self._models) and self._models <= self._tokens.keys()
            return {
                "llm_calls": len(self._models),
                "query_steps": self._queries,
                "total_tokens": sum(self._tokens.values()) if complete else None,
                "events": list(self._events),
            }
