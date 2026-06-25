"""Request builder for DeepSeek provider (OpenAI-compatible chat completions)."""

from typing import Any

from loguru import logger

from core.anthropic import ReasoningReplayMode, build_base_request_body
from core.anthropic.conversion import OpenAIConversionError
from providers.exceptions import InvalidRequestError


def _move_system_to_user(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """DeepSeek rejects ``role: system`` in messages; prepend it to the first user message."""
    if not messages:
        return messages

    system_indices = [
        i for i, m in enumerate(messages) if m.get("role") == "system"
    ]
    if not system_indices:
        return messages

    logger.debug(
        "DEEPSEEK_MOVE_SYSTEM: removing system msgs at indices={} total_msgs={}",
        system_indices,
        len(messages),
    )
    system_texts: list[str] = []
    for i in reversed(system_indices):
        content = messages[i].get("content", "")
        if content:
            if isinstance(content, list):
                content = "\n\n".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )
            system_texts.insert(0, str(content))
        messages.pop(i)

    if not system_texts:
        logger.debug("DEEPSEEK_MOVE_SYSTEM: no system text to move, stripped empty system msgs")
        return messages

    system_block = "\n\n".join(system_texts)
    for msg in messages:
        if msg.get("role") == "user":
            existing = msg["content"]
            if isinstance(existing, list):
                existing = "\n\n".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in existing
                )
            msg["content"] = f"[System]\n{system_block}\n\n[User]\n{existing}"
            logger.debug("DEEPSEEK_MOVE_SYSTEM: merged system into existing user msg")
            return messages

    messages.insert(0, {"role": "user", "content": f"[System]\n{system_block}"})
    logger.debug("DEEPSEEK_MOVE_SYSTEM: inserted new user msg with system content")
    return messages


def build_request_body(request_data: Any, *, thinking_enabled: bool) -> dict:
    """Build OpenAI-format request body from Anthropic request for DeepSeek."""
    logger.debug(
        "DEEPSEEK_REQUEST: conversion start model={} msgs={}",
        getattr(request_data, "model", "?"),
        len(getattr(request_data, "messages", [])),
    )
    try:
        body = build_base_request_body(
            request_data,
            reasoning_replay=ReasoningReplayMode.REASONING_CONTENT,
        )
    except OpenAIConversionError as exc:
        raise InvalidRequestError(str(exc)) from exc

    body["messages"] = _move_system_to_user(body.get("messages", []))

    logger.debug(
        "DEEPSEEK_REQUEST: conversion done model={} msgs={} tools={}",
        body.get("model"),
        len(body.get("messages", [])),
        len(body.get("tools", [])),
    )
    return body
