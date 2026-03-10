"""Unified per-turn runtime context shared across prompt, tools, and coordinators."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nanobot.agent.prompt_context import PromptContext, PromptPurpose
from nanobot.bus.events import InboundMessage
from nanobot.session.manager import Session


@dataclass(slots=True)
class TurnRuntime:
    purpose: PromptPurpose = "chat"
    channel: str = ""
    chat_id: str = ""
    sender_id: str = ""
    session_key: str = ""
    mode: str = ""
    pending_write: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def thread_id(self) -> str:
        return str(self.metadata.get("thread_id") or self.metadata.get("root_id") or "").strip()

    @property
    def quoted_bot_summary(self) -> str:
        value = self.metadata.get("quoted_bot_summary")
        return str(value).strip() if value else ""

    @property
    def recent_selected_table(self) -> dict[str, Any]:
        value = self.metadata.get("recent_selected_table")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def recent_directory_hits(self) -> list[dict[str, Any]]:
        value = self.metadata.get("recent_directory_hits")
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    @property
    def referenced_message(self) -> dict[str, Any]:
        value = self.metadata.get("referenced_message")
        if isinstance(value, dict):
            return dict(value)
        summary = self.quoted_bot_summary
        if summary:
            return {"summary": summary}
        return {}

    @property
    def recent_case_objects(self) -> list[dict[str, Any]]:
        value = self.metadata.get("recent_case_objects")
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    @property
    def recent_contract_objects(self) -> list[dict[str, Any]]:
        value = self.metadata.get("recent_contract_objects")
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    @property
    def recent_weekly_plan_objects(self) -> list[dict[str, Any]]:
        value = self.metadata.get("recent_weekly_plan_objects")
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    @classmethod
    def from_message(
        cls,
        msg: InboundMessage,
        *,
        session: Session | None = None,
        purpose: PromptPurpose = "chat",
        mode: str = "",
    ) -> "TurnRuntime":
        session_metadata = dict(session.metadata or {}) if session is not None else {}
        message_metadata = dict(msg.metadata or {})
        metadata = dict(session_metadata)
        metadata.update({k: v for k, v in message_metadata.items() if v not in (None, "")})
        metadata["channel"] = msg.channel
        metadata["chat_id"] = msg.chat_id
        metadata["sender_id"] = msg.sender_id

        pending_write = isinstance(session_metadata.get("pending_write"), dict)
        return cls(
            purpose=purpose,
            channel=msg.channel,
            chat_id=msg.chat_id,
            sender_id=msg.sender_id,
            session_key=(session.key if session is not None else msg.session_key),
            mode=mode,
            pending_write=pending_write,
            metadata=metadata,
        )

    @classmethod
    def from_session(cls, session: Session, *, purpose: PromptPurpose = "chat", mode: str = "") -> "TurnRuntime":
        metadata = dict(session.metadata or {})
        pending_write = isinstance(metadata.get("pending_write"), dict)
        return cls(
            purpose=purpose,
            channel=str(metadata.get("channel") or ""),
            chat_id=str(metadata.get("chat_id") or ""),
            sender_id=str(metadata.get("sender_id") or ""),
            session_key=session.key,
            mode=mode,
            pending_write=pending_write,
            metadata=metadata,
        )

    def to_prompt_context(self) -> PromptContext:
        return PromptContext(
            purpose=self.purpose,
            channel=self.channel or None,
            chat_id=self.chat_id or None,
            sender_id=self.sender_id or None,
            session_key=self.session_key or None,
            metadata=dict(self.metadata),
        )
