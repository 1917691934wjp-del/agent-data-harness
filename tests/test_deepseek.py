"""DeepSeek configuration and wire compatibility without external requests."""

import json

import httpx
import pytest

from data_agent.config import LLMConfig
from data_agent.config_loader import ConfigLoader
from data_agent.llm import get_llm
from data_agent.llm.provider import DeepSeekChatModel, DeepSeekProvider
from data_agent.models.outputs import SQLGeneratorOutput


def test_builtin_configs_use_deepseek(monkeypatch):
    monkeypatch.setenv("DATA_AGENT_SAMPLE_MODE", "true")
    cfg = ConfigLoader.load_all()
    assert cfg.intent_detection.llm_config.provider == "deepseek"
    assert len(cfg.data_agents) == 6
    for agent in cfg.data_agents:
        assert agent.llm_config.provider == "deepseek"
        assert agent.llm_config.model == "deepseek-flash"
        assert agent.llm_config.max_tokens == 4096
    assert LLMConfig().provider == "deepseek"


def test_factory_uses_deepseek_key_and_endpoint(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = get_llm("deepseek", deployment_name="deepseek-flash", max_tokens=4096)
    assert isinstance(model, DeepSeekChatModel)
    assert model.openai_api_key.get_secret_value() == "offline-test-key"
    assert model.openai_api_base == "https://api.deepseek.com"
    assert model.extra_body == {"thinking": {"type": "disabled"}}


def test_missing_key_has_actionable_error(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        DeepSeekProvider().create_llm()


@pytest.mark.asyncio
async def test_structured_sql_request_and_response_are_compatible():
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert payload["model"] == "deepseek-flash"
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["max_tokens"] == 4096
        assert "max_completion_tokens" not in payload
        assert "response_format" not in payload
        assert payload["tools"][0]["function"]["name"] == "SQLGeneratorOutput"
        return httpx.Response(
            200,
            json={
                "id": "offline-response",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "offline-tool",
                                    "type": "function",
                                    "function": {
                                        "name": "SQLGeneratorOutput",
                                        "arguments": json.dumps({
                                            "thinking": "",
                                            "sql_query": "SELECT 1",
                                            "explanation": "count",
                                        }),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    transport = httpx.MockTransport(respond)
    with httpx.Client(transport=transport) as sync_client:
        async with httpx.AsyncClient(transport=transport) as async_client:
            model = DeepSeekChatModel(
                model="deepseek-flash",
                api_key="offline-test-key",
                base_url="https://api.deepseek.com",
                max_tokens=4096,
                extra_body={"thinking": {"type": "disabled"}},
                use_responses_api=False,
                http_client=sync_client,
                http_async_client=async_client,
            )
            result = await model.with_structured_output(SQLGeneratorOutput).ainvoke(
                "Count"
            )
    assert result.sql_query == "SELECT 1"
    assert len(requests) == 1
