"""Shared helpers for robust LLM required-tool workflows."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from loguru import logger

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

_TOOL_CHOICE_ERROR_MARKERS = (
    "tool_choice",
    "toolchoice",
    "does not support",
    'should be ["none", "auto"]',
)


@dataclass(slots=True)
class RequiredToolCallResult:
    """Normalized result for a required tool call."""

    response: LLMResponse
    arguments: dict[str, Any] | None = None
    error: str | None = None
    missing_fields: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.error is None and self.arguments is not None


def coerce_tool_text(value: Any) -> str:
    """Normalize tool-call payload values to text for storage."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def normalize_tool_arguments(args: Any) -> dict[str, Any] | None:
    """Normalize provider tool-call arguments to the expected dict shape."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None
    if isinstance(args, list):
        return args[0] if args and isinstance(args[0], dict) else None
    return args if isinstance(args, dict) else None


def is_tool_choice_unsupported(content: str | None) -> bool:
    """Detect provider errors caused by forced tool_choice being unsupported."""
    text = (content or "").lower()
    return any(marker in text for marker in _TOOL_CHOICE_ERROR_MARKERS)


def _required_tool_choice(tool_name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": tool_name}}


def _find_tool_call(response: LLMResponse, tool_name: str) -> ToolCallRequest | None:
    for tool_call in response.tool_calls:
        if tool_call.name == tool_name:
            return tool_call
    return None


async def run_required_tool_call(
    provider: LLMProvider,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_name: str,
    required_fields: Iterable[str] = (),
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    reasoning_effort: str | None = None,
    purpose: str | None = None,
    progress_callback: Any | None = None,
    force_tool_choice: bool = True,
) -> RequiredToolCallResult:
    """Call the provider and require that a specific tool is returned."""

    request_kwargs = {
        "messages": messages,
        "tools": tools,
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "reasoning_effort": reasoning_effort,
        "purpose": purpose,
        "progress_callback": progress_callback,
    }

    initial_tool_choice = _required_tool_choice(tool_name) if force_tool_choice else None
    response = await provider.chat_with_retry(
        **request_kwargs,
        tool_choice=initial_tool_choice,
    )

    if force_tool_choice and response.finish_reason == "error" and is_tool_choice_unsupported(response.content):
        logger.warning("Forced tool_choice unsupported for {}, retrying with auto", tool_name)
        response = await provider.chat_with_retry(
            **request_kwargs,
            tool_choice="auto",
        )

    tool_call = _find_tool_call(response, tool_name)
    if tool_call is None:
        return RequiredToolCallResult(
            response=response,
            error="missing_tool_call",
        )

    arguments = normalize_tool_arguments(tool_call.arguments)
    if arguments is None:
        return RequiredToolCallResult(
            response=response,
            error="invalid_arguments",
        )

    missing_fields = tuple(field for field in required_fields if field not in arguments)
    if missing_fields:
        return RequiredToolCallResult(
            response=response,
            arguments=arguments,
            error="missing_required_fields",
            missing_fields=missing_fields,
        )

    null_fields = tuple(field for field in required_fields if arguments.get(field) is None)
    if null_fields:
        return RequiredToolCallResult(
            response=response,
            arguments=arguments,
            error="null_required_fields",
            missing_fields=null_fields,
        )

    return RequiredToolCallResult(
        response=response,
        arguments=arguments,
    )
