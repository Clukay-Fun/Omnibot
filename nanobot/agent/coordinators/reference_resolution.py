"""Coordinator for deterministic reference lookups from the conversation frame."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanobot.agent.coordinators.base import AgentCoordinator
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.session.manager import Session

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop


class ReferenceResolutionCoordinator(AgentCoordinator):
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

    async def handle(self, *, msg: InboundMessage, session: Session) -> OutboundMessage | None:
        text = msg.content.strip()
        metadata = dict(session.metadata or {})

        if any(token in text for token in ("刚才那个表", "上一个表", "最近那个表")) and any(
            token in text for token in ("是什么", "哪个", "是哪张")
        ):
            table = metadata.get("recent_selected_table") if isinstance(metadata.get("recent_selected_table"), dict) else {}
            if table:
                table_name = str(table.get("table_name") or table.get("name") or table.get("table_id") or "").strip()
                table_id = str(table.get("table_id") or "").strip()
                if table_name or table_id:
                    label = table_name or table_id
                    if table_name and table_id:
                        label = f"{table_name}（{table_id}）"
                    content = f"刚才选中的表是：{label}。"
                    self._record_direct_turn(session, msg, content)
                    self._loop.sessions.save(session)
                    return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})

        if any(token in text for token in ("那条消息", "引用的消息", "刚才那条消息")) and any(
            token in text for token in ("说了什么", "是什么", "内容")
        ):
            referenced = metadata.get("referenced_message") if isinstance(metadata.get("referenced_message"), dict) else {}
            summary = str(referenced.get("summary") or metadata.get("quoted_bot_summary") or "").strip()
            if summary:
                content = f"那条消息的摘要是：{summary}"
                self._record_direct_turn(session, msg, content)
                self._loop.sessions.save(session)
                return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})

        return None
