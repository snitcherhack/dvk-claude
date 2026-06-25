"""Tests for DeepSeek OpenAI-compatible chat completions provider."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.models.anthropic import Message, MessagesRequest
from providers.base import ProviderConfig
from providers.deepseek import DEEPSEEK_DEFAULT_BASE, DeepSeekProvider


@pytest.fixture
def deepseek_config():
    return ProviderConfig(
        api_key="test_deepseek_key",
        base_url=DEEPSEEK_DEFAULT_BASE,
        rate_limit=10,
        rate_window=60,
        enable_thinking=True,
    )


@pytest.fixture
def deepseek_provider(deepseek_config):
    return DeepSeekProvider(deepseek_config)


def test_default_base_url():
    assert DEEPSEEK_DEFAULT_BASE == "https://api.deepseek.com/v1"


def test_init_uses_correct_base_url(deepseek_config):
    provider = DeepSeekProvider(deepseek_config)
    assert provider._api_key == "test_deepseek_key"
    assert provider._base_url == "https://api.deepseek.com/v1"


def test_init_defaults_base_url_when_not_configured():
    provider = DeepSeekProvider(
        ProviderConfig(api_key="k", rate_limit=1, rate_window=1)
    )
    assert provider._base_url == "https://api.deepseek.com/v1"


def test_build_request_body_openai_shape(deepseek_provider):
    """DeepSeek rejects role=system; system content is prepended to first user message."""
    request = MessagesRequest(
        model="deepseek-chat",
        max_tokens=100,
        messages=[Message(role="user", content="Hello")],
        system="You are helpful.",
    )
    body = deepseek_provider._build_request_body(request)
    assert body["model"] == "deepseek-chat"
    assert body["messages"][0]["role"] == "user"
    assert "[System]\nYou are helpful." in body["messages"][0]["content"]
    assert "Hello" in body["messages"][0]["content"]
    assert body["max_tokens"] == 100


def test_build_request_body_no_default_max_tokens(deepseek_provider):
    """OpenAI format does not require max_tokens; absent when not in request."""
    request = MessagesRequest(
        model="m",
        messages=[Message(role="user", content="x")],
    )
    body = deepseek_provider._build_request_body(request)
    assert "max_tokens" not in body


def test_build_request_body_converts_tools(deepseek_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [
                {
                    "name": "Read",
                    "description": "Read a file",
                    "input_schema": {
                        "type": "object",
                        "properties": {"file_path": {"type": "string"}},
                        "required": ["file_path"],
                    },
                }
            ],
        }
    )
    body = deepseek_provider._build_request_body(request)
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "Read"


def test_build_request_body_converts_tool_choice(deepseek_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "tool_choice": {"type": "auto"},
        }
    )
    body = deepseek_provider._build_request_body(request)
    assert body["tool_choice"] == "auto"


def test_build_request_body_includes_temperature(deepseek_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "temperature": 0.7,
        }
    )
    body = deepseek_provider._build_request_body(request)
    assert body["temperature"] == 0.7


def test_build_request_body_includes_stop_sequences(deepseek_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "stop_sequences": ["END"],
        }
    )
    body = deepseek_provider._build_request_body(request)
    assert body["stop"] == ["END"]


def test_build_request_body_tool_result_normalized_to_string(deepseek_provider):
    """Tool result content arrays are converted to strings by the OpenAI converter."""
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Read",
                            "input": {"file_path": "f"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [
                                {"type": "text", "text": "file content here"}
                            ],
                        }
                    ],
                },
            ],
        }
    )
    body = deepseek_provider._build_request_body(request)
    tool_msg = body["messages"][1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["content"] == "file content here"


@pytest.mark.asyncio
async def test_stream_uses_chat_completions_path(deepseek_provider):
    request = MessagesRequest(
        model="m",
        messages=[Message(role="user", content="hi")],
    )
    called: dict[str, str] = {}

    async def fake_create(**kwargs):
        called["stream"] = str(kwargs.get("stream", False))
        mock_stream = MagicMock()

        async def aiter():
            if False:
                yield

        mock_stream.__aiter__ = aiter
        return mock_stream

    with patch.object(
        deepseek_provider._client.chat.completions, "create", side_effect=fake_create
    ):
        _ = [x async for x in deepseek_provider.stream_response(request, request_id="r1")]

    assert called["stream"] == "True"
