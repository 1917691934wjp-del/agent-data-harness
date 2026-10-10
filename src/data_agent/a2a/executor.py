"""A2A Executor for the NL2SQL Data Agent.

This module provides the AgentExecutor implementation that bridges the
A2A protocol with the DataAgentFlow for processing natural language queries.
"""

import logging
from typing import TYPE_CHECKING, Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    InternalError,
    InvalidParamsError,
    Part,
    TaskState,
    TextPart,
    UnsupportedOperationError,
)
from a2a.utils import (
    new_agent_text_message,
    new_task,
)
from a2a.utils.errors import ServerError

from data_agent.agent import DataAgentFlow
from data_agent.config import AgentConfig
from data_agent.harness import AgentHarness

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from a2a.types import Task


class DataAgentExecutor(AgentExecutor):
    """Data Agent A2A Executor for NL2SQL queries."""

    def __init__(
        self,
        config_path: str | None = None,
        config: AgentConfig | None = None,
    ) -> None:
        """Initialize the executor.

        Args:
            config_path: Path to agent configuration file.
            config: Pre-loaded AgentConfig instance.
                Takes precedence over config_path if both provided.
        """
        self.agent = DataAgentFlow(config_path=config_path, config=config)
        self._connected = False

    async def _ensure_connected(self) -> None:
        """Ensure the agent is connected to all datasources."""
        if not self._connected:
            await self.agent.connect()
            self._connected = True

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Execute a natural language query via the A2A protocol.

        Args:
            context: Request context containing the user message and task info.
            event_queue: Queue for sending task status updates and artifacts.

        Raises:
            ServerError: If request validation fails or query execution errors.
        """
        error = self._validate_request(context)
        if error:
            raise ServerError(error=InvalidParamsError())

        await self._ensure_connected()

        task = context.current_task
        if not task:
            task = new_task(context.message)  # type: ignore
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)

        try:
            await updater.update_status(
                TaskState.working,
                new_agent_text_message(
                    "Processing your query...",
                    task.context_id,
                    task.id,
                ),
            )

            result = await self._run_request(context, task)
            await self._publish_result(updater, task, result)

        except ServerError:
            raise
        except Exception as e:
            logger.error(f"An error occurred while executing the query: {e}")
            raise ServerError(error=InternalError()) from e

    async def _run_request(
        self, context: RequestContext, task: "Task"
    ) -> dict[str, Any]:
        """Require explicit versioned decisions when the graph awaits review."""
        pending = await self.agent.get_pending_input(task.context_id)
        decision = (context.message.metadata or {}).get("review_decision")
        query = context.get_user_input()
        if pending:
            if context.current_task is None:
                raise ServerError(error=InvalidParamsError())
            if pending.get("type") == "plan_review":
                reply = decision if isinstance(decision, dict) else {}
            else:
                reply = {"question": query}
            return dict(
                await AgentHarness(self.agent).run_output(
                    thread_id=task.context_id, resume_value=reply
                )
            )
        if decision is not None:
            raise ServerError(error=InvalidParamsError())
        return await AgentHarness(self.agent).run_output(
            question=query, thread_id=task.context_id
        )

    @staticmethod
    async def _publish_result(
        updater: TaskUpdater, task: "Task", result: dict[str, Any]
    ) -> None:
        """An interrupt ends this response stream, not the underlying task."""
        if result.get("__interrupt__"):
            payload = result["__interrupt__"][0].value
            message = new_agent_text_message(
                payload["message"] + "\n" + payload.get("hint", ""),
                task.context_id,
                task.id,
            )
            message.metadata = {"human_input": payload}
            await updater.update_status(TaskState.input_required, message, final=True)
            return
        if result.get("error"):
            state = (
                TaskState.canceled
                if result["error"] == "cancelled"
                else TaskState.failed
            )
            await updater.update_status(
                state,
                new_agent_text_message(
                    result.get("final_response") or str(result["error"]),
                    task.context_id,
                    task.id,
                ),
                final=True,
            )
            return
        await updater.add_artifact(
            [
                Part(
                    root=TextPart(text=result.get("final_response", "Query completed."))
                )
            ],
            name="query_result",
        )
        await updater.complete()

    @staticmethod
    def _validate_request(context: RequestContext) -> bool:
        """Validate the incoming request.

        Args:
            context: Request context to validate.

        Returns:
            True if validation fails, False if valid.
        """
        return context.message is None

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Cancel a task paused for human input.

        Args:
            context: Request context for the task to cancel.
            event_queue: Queue for sending cancellation events.

        Raises:
            ServerError: Running-query cancellation is not supported; paused
                clarification and plan review can be cancelled before execution.
        """
        task = context.current_task
        if task is None:
            raise ServerError(error=InvalidParamsError())
        pending = await self.agent.get_pending_input(task.context_id)
        if pending is None:
            raise ServerError(error=UnsupportedOperationError())
        result = await AgentHarness(self.agent).run_output(
            thread_id=task.context_id,
            resume_value={
                "action": "cancel",
                "plan_version": pending.get("plan_version", ""),
            },
        )
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        if result.get("error") != "cancelled":
            await self._publish_result(updater, task, dict(result))
            return
        await updater.cancel(
            new_agent_text_message("Query cancelled.", task.context_id, task.id)
        )
