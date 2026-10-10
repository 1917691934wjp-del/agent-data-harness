"""LLM provider implementations.

This module contains concrete provider implementations for different
LLM services like Azure OpenAI.
"""

import os
from typing import Any

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel
from langchain_openai import AzureChatOpenAI, ChatOpenAI

from data_agent.llm.base import BaseProvider


class DeepSeekChatModel(ChatOpenAI):
    """Use tool calling instead of OpenAI-specific JSON schema responses."""

    def with_structured_output(self, schema: Any = None, **kwargs: Any) -> Any:  # noqa: ANN401 - preserve LangChain schema/Runnable API
        kwargs.setdefault("method", "function_calling")
        kwargs.setdefault("strict", False)
        return super().with_structured_output(schema, **kwargs)

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,  # noqa: ANN401 - LangChain request adapter
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        payload.pop("parallel_tool_calls", None)
        return payload


class DeepSeekProvider(BaseProvider):
    name = "deepseek"

    def create_llm(self, **kwargs: Any) -> BaseChatModel:  # noqa: ANN401, PLR6301 - provider interface
        load_dotenv()
        key = kwargs.get("api_key") or os.getenv("DEEPSEEK_API_KEY")
        if not key or not key.strip():
            raise ValueError(
                "DeepSeek requires DEEPSEEK_API_KEY in the environment or .env"
            )
        options = {
            "model": kwargs.get("deployment_name")
            or os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
            "api_key": key,
            "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            "temperature": kwargs.get("temperature", 0),
            "extra_body": {"thinking": {"type": "disabled"}},
            "use_responses_api": False,
        }
        if kwargs.get("max_tokens") is not None:
            options["max_tokens"] = kwargs["max_tokens"]
        return DeepSeekChatModel(**options)


class AzureOpenAIProvider(BaseProvider):
    """Azure OpenAI LLM provider.

    Attributes:
        name: Provider identifier ('azure_openai').
    """

    name = "azure_openai"

    def create_llm(self, **kwargs: Any) -> BaseChatModel:
        """Create an Azure OpenAI chat model.

        Args:
            azure_endpoint: Azure OpenAI endpoint URL.
            api_key: Azure OpenAI API key.
            deployment_name: Name of the deployed model.
            api_version: API version (default: '2024-08-01-preview').
            temperature: Sampling temperature (default: 0).
            **kwargs: Additional AzureChatOpenAI parameters.

        Returns:
            Configured AzureChatOpenAI instance.
        """
        return AzureChatOpenAI(
            azure_endpoint=kwargs.get("azure_endpoint"),
            api_key=kwargs.get("api_key"),
            azure_deployment=kwargs.get("deployment_name"),
            api_version=kwargs.get("api_version", "2024-08-01-preview"),
            temperature=kwargs.get("temperature", 0),
        )
