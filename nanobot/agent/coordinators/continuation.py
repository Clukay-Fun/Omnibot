"""Coordinator for deterministic continuation/pagination commands."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.coordinators.base import AgentCoordinator
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.session.manager import Session

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop


class ContinuationCoordinator(AgentCoordinator):
    _COMMANDS = {"继续", "更多", "下一页", "more", "continue", "next"}

    def __init__(self, agent: "AgentLoop") -> None:
        super().__init__(agent)

    @property
    def _loop(self) -> "AgentLoop":
        assert self._agent is not None
        return self._agent

    @staticmethod
    def _record_direct_turn(session: Session, msg: InboundMessage, assistant_content: str) -> None:
        session.add_message("user", msg.content)
        session.add_message("assistant", assistant_content)

    @classmethod
    def _is_continuation(cls, text: str) -> bool:
        return text.strip().lower() in cls._COMMANDS

    @staticmethod
    def _format_directory_contacts(contacts: list[dict[str, Any]], *, remaining: int = 0) -> str:
        if not contacts:
            return "没有更多联系人可展示了。"
        lines = ["继续展示联系人："]
        for item in contacts:
            name = str(item.get("display_name") or item.get("open_id") or "未命名联系人").strip()
            open_id = str(item.get("open_id") or "").strip()
            parts = [name]
            if open_id:
                parts.append(f"open_id: {open_id}")
            lines.append(f"- {'；'.join(parts)}")
        if remaining > 0:
            lines.append(f"\n还有 {remaining} 条，回复“继续”查看。")
        return "\n".join(lines)

    @staticmethod
    def _format_selection_options(
        selection: dict[str, Any],
        items: list[dict[str, Any]],
        *,
        remaining: int = 0,
        start_index: int = 1,
    ) -> str:
        kind = str(selection.get("kind") or "options")
        title = "继续展示候选项：" if kind == "table_candidates" else "继续展示可选项："
        if kind == "record_candidates":
            title = "继续展示候选记录："
        lines = [title]
        for idx, item in enumerate(items, start=start_index):
            label = str(
                item.get("name")
                or item.get("display_name")
                or item.get("table_id")
                or item.get("open_id")
                or item.get("record_id")
                or "未命名项"
            )
            lines.append(f"- {idx}. {label}")
        if remaining > 0:
            lines.append(f"\n还有 {remaining} 条，回复“继续”查看。")
        return "\n".join(lines)

    async def handle(self, *, msg: InboundMessage, session: Session) -> OutboundMessage | None:
        if not self._is_continuation(msg.content):
            return None

        metadata = dict(session.metadata or {})
        selection = metadata.get("result_selection") if isinstance(metadata.get("result_selection"), dict) else {}
        offset = int(selection.get("offset") or 0) if selection else 0
        page_size = max(1, int(selection.get("page_size") or 5)) if selection else 5
        had_local_continuation_state = bool(selection) or bool(metadata.get("recent_directory_hits"))

        if selection and isinstance(selection.get("items"), list):
            items = [dict(item) for item in selection.get("items", []) if isinstance(item, dict)]
            if offset < len(items):
                next_items = items[offset : offset + page_size]
                start_index = offset + 1
                selection["offset"] = min(len(items), offset + page_size)
                metadata["result_selection"] = selection
                session.metadata = metadata
                remaining = max(0, len(items) - int(selection["offset"]))
                content = self._format_selection_options(
                    selection,
                    next_items,
                    remaining=remaining,
                    start_index=start_index,
                )
                self._record_direct_turn(session, msg, content)
                self._loop.sessions.save(session)
                return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})

        contacts = metadata.get("recent_directory_hits") if isinstance(metadata.get("recent_directory_hits"), list) else []
        offset = int(metadata.get("recent_directory_offset") or 0)
        if contacts and offset < len(contacts):
            next_contacts = [dict(item) for item in contacts[offset : offset + page_size] if isinstance(item, dict)]
            offset = min(len(contacts), offset + page_size)
            metadata["recent_directory_offset"] = offset
            session.metadata = metadata
            remaining = max(0, len(contacts) - offset)
            content = self._format_directory_contacts(next_contacts, remaining=remaining)
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})

        if not had_local_continuation_state:
            return None

        content = self._loop._runtime_text.prompt_text("pagination", "no_more_content", "没有可继续的内容了。")
        self._record_direct_turn(session, msg, content)
        self._loop.sessions.save(session)
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})
