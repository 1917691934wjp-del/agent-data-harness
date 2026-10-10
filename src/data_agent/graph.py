"""LangGraph pipeline builder for the NL2SQL Data Agent.

This module provides the DataAgentGraph class for creating LangGraph pipelines
for natural language to SQL query conversion.
"""

import logging
from typing import TYPE_CHECKING, Literal, Union

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from data_agent.config import DataAgentConfig
from data_agent.context import SchemaSnapshot
from data_agent.executors import create_executor
from data_agent.models.state import AgentState, InputState, OutputState
from data_agent.nodes.data_nodes import DataAgentNodes
from data_agent.nodes.response import ResponseNode
from data_agent.nodes.visualization import VisualizationNode

if TYPE_CHECKING:
    from langchain_community.utilities.sql_database import SQLDatabase

    from data_agent.adapters.azure.cosmos import CosmosAdapter

logger = logging.getLogger(__name__)


class DataAgentGraph:
    """LangGraph pipeline for NL2SQL query generation and execution.

    This class encapsulates the graph construction logic for the data agent,
    handling SQL generation, validation, execution, and response generation.

    Attributes:
        llm: Language model for SQL and response generation.
        datasource: SQLDatabase or CosmosAdapter instance.
        config: Data agent configuration with schema and prompts.
        max_retries: Maximum SQL generation retry attempts.
    """

    def __init__(
        self,
        llm: BaseChatModel,
        datasource: Union["SQLDatabase", "CosmosAdapter"],
        config: DataAgentConfig,
        max_retries: int = 3,
        *,
        schema_snapshot: SchemaSnapshot | None = None,
    ) -> None:
        """Initialize the data agent graph.

        Args:
            llm: Language model for SQL and response generation.
            datasource: SQLDatabase or CosmosAdapter instance.
            config: Data agent configuration with schema and prompts.
            max_retries: Maximum SQL generation retry attempts.
        """
        self.llm = llm
        self.datasource = datasource
        self.config = config
        self.max_retries = max_retries

        self._nodes = DataAgentNodes(
            llm, datasource, config, max_retries, schema_snapshot=schema_snapshot
        )
        self._response_node = ResponseNode(llm, config)

        # Initialize visualization node
        executor = create_executor()
        self._viz_node = VisualizationNode(llm, executor)

    def _should_retry(self, state: AgentState) -> str:
        """Determine if SQL generation should be retried.

        Args:
            state: Current agent state.

        Returns:
            'retry' if error is recoverable and retries remain.
            'execute' if no error (validation passed).
            'end' if max retries exceeded or unrecoverable error.
        """
        error = state.get("error")
        if not error:
            return "execute"
        if "failed after" in str(error) or "Max retries" in str(error):
            return "end"
        return "retry"

    def _route_after_execute(self, state: AgentState) -> str:
        """Route after query execution based on error and visualization request.

        Args:
            state: Current agent state.

        Returns:
            'error' on failure, 'complete' for evidence-only workers,
            'visualize' for charts, or 'respond' for a single-agent answer.
        """
        if state.get("error"):
            return "error"
        if state.get("skip_response"):
            return "complete"
        if state.get("visualization_requested", False):
            return "visualize"
        return "respond"

    def build(self) -> StateGraph:
        """Build the state graph without compiling.

        Returns:
            Uncompiled StateGraph instance.
        """
        graph = StateGraph(
            AgentState, input_schema=InputState, output_schema=OutputState
        )

        graph.add_node("generate_sql", self._nodes.generate_sql)
        graph.add_node("validate_sql", self._nodes.validate_sql)
        graph.add_node("retry_sql", self._nodes.retry_sql)
        graph.add_node("execute_query", self._nodes.execute_query)
        graph.add_node("generate_response", self._response_node.generate_response)
        graph.add_node("visualize_data", self._viz_node.generate_visualization)

        graph.set_entry_point("generate_sql")

        graph.add_edge("generate_sql", "validate_sql")
        graph.add_conditional_edges(
            "validate_sql",
            self._should_retry,
            {"retry": "retry_sql", "execute": "execute_query", "end": END},
        )
        graph.add_edge("retry_sql", "validate_sql")
        graph.add_conditional_edges(
            "execute_query",
            self._route_after_execute,
            {
                "error": END,
                "complete": END,
                "visualize": "visualize_data",
                "respond": "generate_response",
            },
        )
        graph.add_edge("visualize_data", "generate_response")
        graph.add_edge("generate_response", END)

        return graph

    def compile(
        self,
        checkpointer: BaseCheckpointSaver | Literal[False] | None = None,
        name: str = "data_agent_graph",
    ) -> CompiledStateGraph:
        """Build and compile the state graph.

        Args:
            checkpointer: Optional checkpointer for conversation persistence.
                False disables worker checkpointing for isolated collaboration.
            name: Name for the compiled graph.

        Returns:
            Compiled StateGraph instance ready for invocation.
        """
        graph = self.build()
        return graph.compile(
            name=name,
            checkpointer=InMemorySaver() if checkpointer is None else checkpointer,
        )


def create_data_agent(
    llm: BaseChatModel,
    datasource: Union["SQLDatabase", "CosmosAdapter"],
    config: DataAgentConfig,
    max_retries: int = 3,
    checkpointer: BaseCheckpointSaver | Literal[False] | None = None,
    *,
    schema_snapshot: SchemaSnapshot | None = None,
) -> CompiledStateGraph:
    """Create a data agent graph for any supported datasource.

    This is a convenience function that wraps DataAgentGraph.

    Args:
        llm: Language model for SQL and response generation.
        datasource: SQLDatabase or CosmosAdapter instance.
        config: Data agent configuration with schema and prompts.
        max_retries: Maximum SQL generation retry attempts.
        checkpointer: Optional checkpointer for conversation persistence.

    Returns:
        Compiled StateGraph instance ready for invocation.
    """
    return DataAgentGraph(
        llm, datasource, config, max_retries, schema_snapshot=schema_snapshot
    ).compile(checkpointer=checkpointer)
