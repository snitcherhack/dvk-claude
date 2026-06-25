"""DeepSeek provider implementation (OpenAI-compatible chat completions)."""

from __future__ import annotations

from typing import Any

from loguru import logger

from providers.base import ProviderConfig
from providers.defaults import DEEPSEEK_DEFAULT_BASE
from providers.openai_compat import OpenAIChatTransport

from .request import build_request_body


class DeepSeekProvider(OpenAIChatTransport):
    """DeepSeek using the OpenAI-compatible ``/v1/chat/completions`` API."""

    def __init__(self, config: ProviderConfig):
        super().__init__(
            config,
            provider_name="DEEPSEEK",
            base_url=config.base_url or DEEPSEEK_DEFAULT_BASE,
            api_key=config.api_key,
        )

    def _build_request_body(
        self, request: Any, thinking_enabled: bool | None = None
    ) -> dict:
        return build_request_body(
            request,
            thinking_enabled=self._is_thinking_enabled(request, thinking_enabled),
        )

    def _prepare_create_body(self, body: dict[str, Any]) -> dict[str, Any]:
        """Safety net: strip system-role msgs and flatten array content to strings.

        DeepSeek v4-pro rejects ``role: system`` and ``content`` as an array
        of content blocks — both must be strings.
        """
        messages = body.get("messages", [])
        system_count = sum(1 for m in messages if m.get("role") == "system")
        if system_count:
            logger.warning(
                "DEEPSEEK_SAFETY_NET: {} system-role msgs survived _move_system_to_user, stripping them",
                system_count,
            )
        cleaned: list[dict[str, Any]] = []
        for m in messages:
            if m.get("role") == "system":
                continue
            content = m.get("content")
            if isinstance(content, list):
                m = {**m, "content": "\n\n".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )}
            cleaned.append(m)
        body["messages"] = cleaned
        return body
