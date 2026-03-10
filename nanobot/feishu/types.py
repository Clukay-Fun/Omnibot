"""Shared Feishu pipeline types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TranslatedFeishuMessage:
    """Normalized inbound Feishu message ready for bus publication."""

    sender_id: str
    chat_id: str
    content: str
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    session_key: str | None = None
