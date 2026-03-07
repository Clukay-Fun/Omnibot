"""Prompt context helpers for runtime-aware workspace loading."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


PromptPurpose = Literal["chat", "heartbeat", "bootstrap"]


@dataclass(slots=True)
class PromptContext:
    """Runtime metadata that influences workspace file selection."""

    purpose: PromptPurpose = "chat"
    channel: str | None = None
    chat_id: str | None = None
    sender_id: str | None = None
    session_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def chat_type(self) -> str:
        return str(self.metadata.get("chat_type") or "")

    @property
    def is_feishu(self) -> bool:
        return self.channel == "feishu"

    @property
    def is_topic(self) -> bool:
        return bool(self.metadata.get("thread_id") or self.metadata.get("root_id") and self.metadata.get("parent_id"))

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"

    @property
    def is_private(self) -> bool:
        return not self.is_group

    @property
    def quoted_bot_summary(self) -> str:
        value = self.metadata.get("quoted_bot_summary")
        return str(value).strip() if value else ""
