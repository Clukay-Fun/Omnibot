"""Memory system for persistent agent memory."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from nanobot.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph (2-5 sentences) summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all existing "
                        "facts plus new ones. Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


def _ensure_text(value: Any) -> str:
    """Normalize tool-call payload values to text for file storage."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _normalize_save_memory_args(args: Any) -> dict[str, Any] | None:
    """Normalize provider tool-call arguments to the expected dict shape."""
    if isinstance(args, str):
        args = json.loads(args)
    if isinstance(args, list):
        return args[0] if args and isinstance(args[0], dict) else None
    return args if isinstance(args, dict) else None


_TOOL_CHOICE_ERROR_MARKERS = (
    "tool_choice",
    "toolchoice",
    "does not support",
    'should be ["none", "auto"]',
)


@dataclass(slots=True)
class MemoryConsolidationResult:
    """Normalized consolidation payload emitted after a successful archive."""

    history_entry: str
    memory_update: str
    raw_archive: bool = False


def _is_tool_choice_unsupported(content: str | None) -> bool:
    """Detect provider errors caused by forced tool_choice being unsupported."""
    text = (content or "").lower()
    return any(marker in text for marker in _TOOL_CHOICE_ERROR_MARKERS)


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    _MAX_FAILURES_BEFORE_RAW_ARCHIVE = 3

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"
        self._consecutive_failures = 0

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    @staticmethod
    def _format_messages(messages: list[dict[str, Any]]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    @staticmethod
    def _select_messages(
        session: Session,
        *,
        archive_all: bool,
        memory_window: int,
    ) -> tuple[list[dict[str, Any]], int] | None:
        start = max(0, min(session.last_consolidated, len(session.messages)))

        if archive_all:
            old_messages = session.messages[start:]
            keep_count = 0
            if not old_messages:
                return None
            logger.info("Memory consolidation (archive_all): {} messages", len(old_messages))
            return old_messages, keep_count

        keep_count = max(1, memory_window // 2)
        if len(session.messages) <= keep_count:
            return None
        if len(session.messages) - start <= 0:
            return None

        old_messages = session.messages[start:-keep_count]
        if not old_messages:
            return None

        logger.info("Memory consolidation: {} to consolidate, {} keep", len(old_messages), keep_count)
        return old_messages, keep_count

    async def consolidate(
        self,
        session: Session,
        provider: LLMProvider,
        model: str,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,
        purpose: str | None = None,
        on_consolidated: Callable[[MemoryConsolidationResult], None] | None = None,
    ) -> bool:
        """Consolidate old messages into MEMORY.md + HISTORY.md via LLM tool call.

        Returns True on success (including no-op), False on failure.
        """
        selection = self._select_messages(
            session,
            archive_all=archive_all,
            memory_window=memory_window,
        )
        if selection is None:
            return True

        old_messages, keep_count = selection
        success = await self.consolidate_messages(
            old_messages,
            provider,
            model,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            purpose=purpose,
            on_consolidated=on_consolidated,
        )
        if not success:
            return False

        session.last_consolidated = 0 if archive_all else len(session.messages) - keep_count
        logger.info(
            "Memory consolidation done: {} messages, last_consolidated={}",
            len(session.messages),
            session.last_consolidated,
        )
        return True

    async def consolidate_messages(
        self,
        messages: list[dict[str, Any]],
        provider: LLMProvider,
        model: str,
        *,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,
        purpose: str | None = None,
        on_consolidated: Callable[[MemoryConsolidationResult], None] | None = None,
    ) -> bool:
        """Consolidate an explicit message chunk into MEMORY.md + HISTORY.md."""
        if not messages:
            return True

        current_memory = self.read_long_term()
        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{self._format_messages(messages)}"""

        try:
            forced = {"type": "function", "function": {"name": "save_memory"}}
            response = await provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."},
                    {"role": "user", "content": prompt},
                ],
                tools=_SAVE_MEMORY_TOOL,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                tool_choice=forced,
                purpose=purpose,
            )

            if response.finish_reason == "error" and _is_tool_choice_unsupported(response.content):
                logger.warning("Forced tool_choice unsupported, retrying memory consolidation with auto")
                response = await provider.chat_with_retry(
                    messages=[
                        {"role": "system", "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."},
                        {"role": "user", "content": prompt},
                    ],
                    tools=_SAVE_MEMORY_TOOL,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    reasoning_effort=reasoning_effort,
                    tool_choice="auto",
                    purpose=purpose,
                )

            if not response.has_tool_calls:
                logger.warning(
                    "Memory consolidation: LLM did not call save_memory "
                    "(finish_reason={}, content_len={}, content_preview={})",
                    response.finish_reason,
                    len(response.content or ""),
                    (response.content or "")[:200],
                )
                return self._fail_or_raw_archive(messages, on_consolidated=on_consolidated)

            args = _normalize_save_memory_args(response.tool_calls[0].arguments)
            if args is None:
                logger.warning("Memory consolidation: unexpected save_memory arguments")
                return self._fail_or_raw_archive(messages, on_consolidated=on_consolidated)

            if "history_entry" not in args or "memory_update" not in args:
                logger.warning("Memory consolidation: save_memory payload missing required fields")
                return self._fail_or_raw_archive(messages, on_consolidated=on_consolidated)

            entry = args["history_entry"]
            update = args["memory_update"]

            if entry is None or update is None:
                logger.warning("Memory consolidation: save_memory payload contains null required fields")
                return self._fail_or_raw_archive(messages, on_consolidated=on_consolidated)

            entry = _ensure_text(entry).strip()
            if not entry:
                logger.warning("Memory consolidation: history_entry is empty after normalization")
                return self._fail_or_raw_archive(messages, on_consolidated=on_consolidated)

            self.append_history(entry)
            update = _ensure_text(update)
            if update != current_memory:
                self.write_long_term(update)
            if on_consolidated is not None:
                on_consolidated(MemoryConsolidationResult(history_entry=entry, memory_update=update))

            self._consecutive_failures = 0
            logger.info("Memory consolidation done for {} messages", len(messages))
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return self._fail_or_raw_archive(messages, on_consolidated=on_consolidated)

    async def archive_messages(
        self,
        messages: list[dict[str, Any]],
        provider: LLMProvider,
        model: str,
        *,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,
        purpose: str | None = None,
        on_consolidated: Callable[[MemoryConsolidationResult], None] | None = None,
    ) -> bool:
        """Archive messages with guaranteed persistence semantics."""
        if not messages:
            return True

        for _ in range(self._MAX_FAILURES_BEFORE_RAW_ARCHIVE):
            if await self.consolidate_messages(
                messages,
                provider,
                model,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                purpose=purpose,
                on_consolidated=on_consolidated,
            ):
                return True
        return True

    def _fail_or_raw_archive(
        self,
        messages: list[dict[str, Any]],
        *,
        on_consolidated: Callable[[MemoryConsolidationResult], None] | None = None,
    ) -> bool:
        """Increment failure count; after threshold, raw-archive messages and return True."""
        self._consecutive_failures += 1
        if self._consecutive_failures < self._MAX_FAILURES_BEFORE_RAW_ARCHIVE:
            return False
        self._raw_archive(messages, on_consolidated=on_consolidated)
        self._consecutive_failures = 0
        return True

    def _raw_archive(
        self,
        messages: list[dict[str, Any]],
        *,
        on_consolidated: Callable[[MemoryConsolidationResult], None] | None = None,
    ) -> None:
        """Fallback: dump raw messages to HISTORY.md without LLM summarization."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"[{ts}] [RAW] {len(messages)} messages\n{self._format_messages(messages)}"
        self.append_history(entry)
        if on_consolidated is not None:
            on_consolidated(MemoryConsolidationResult(history_entry=entry, memory_update=self.read_long_term(), raw_archive=True))
        logger.warning("Memory consolidation degraded: raw-archived {} messages", len(messages))
