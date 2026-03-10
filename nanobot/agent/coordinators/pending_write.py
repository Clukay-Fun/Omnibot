"""Coordinator for structured write confirmations in normal chat."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.coordinators.base import AgentCoordinator, CoordinatorToolResult
from nanobot.agent.pending_write import (
    PENDING_WRITE_METADATA_KEY,
    coerce_pending_write_result,
    extract_json_object,
    extract_pending_write_command,
    format_pending_write_preview,
)
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.session.manager import Session

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop


class PendingWriteCoordinator(AgentCoordinator):
    def __init__(self, agent: AgentLoop) -> None:
        super().__init__(agent)

    @property
    def _loop(self) -> AgentLoop:
        assert self._agent is not None
        return self._agent

    @staticmethod
    def _pending_write(session: Session) -> dict[str, Any]:
        value = session.metadata.get(PENDING_WRITE_METADATA_KEY)
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _set_pending_write(session: Session, payload: dict[str, Any]) -> None:
        metadata = dict(session.metadata or {})
        metadata[PENDING_WRITE_METADATA_KEY] = payload
        session.metadata = metadata

    @staticmethod
    def _clear_pending_write(session: Session) -> None:
        metadata = dict(session.metadata or {})
        metadata.pop(PENDING_WRITE_METADATA_KEY, None)
        session.metadata = metadata

    @staticmethod
    def _record_direct_turn(session: Session, msg: InboundMessage, assistant_content: str) -> None:
        session.add_message("user", msg.content)
        session.add_message("assistant", assistant_content)

    @staticmethod
    def _pending_write_args_from_payload(
        *,
        tool_name: str,
        raw_args: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        args = dict(raw_args)
        args.pop("confirm_token", None)
        preview: dict[str, Any] = payload.get("preview") if isinstance(payload.get("preview"), dict) else {}
        if isinstance(preview.get("fields"), dict):
            args["fields"] = dict(preview["fields"])
        for key in ("table_id", "record_id"):
            value = preview.get(key)
            if value not in (None, ""):
                args[key] = value
        if tool_name == "bitable_delete" and "record_id" in raw_args:
            args.setdefault("record_id", raw_args.get("record_id"))
        return args

    def on_tool_result(
        self,
        *,
        session: Session,
        tool_name: str,
        raw_args: dict[str, Any],
        result: str,
    ) -> CoordinatorToolResult | None:
        payload = extract_json_object(result)
        if not payload or payload.get("dry_run") is not True or not payload.get("confirm_token"):
            return None
        preview: dict[str, Any] = payload.get("preview") if isinstance(payload.get("preview"), dict) else {}
        if preview.get("table_id"):
            metadata = dict(session.metadata or {})
            previous: dict[str, Any] = (
                metadata.get("recent_selected_table") if isinstance(metadata.get("recent_selected_table"), dict) else {}
            )
            metadata["recent_selected_table"] = {
                **dict(previous),
                "table_id": str(preview.get("table_id") or ""),
                "table_name": str(previous.get("table_name") or previous.get("name") or "").strip(),
            }
            session.metadata = metadata
        pending = {
            "tool": tool_name,
            "token": str(payload.get("confirm_token") or ""),
            "args": self._pending_write_args_from_payload(tool_name=tool_name, raw_args=raw_args, payload=payload),
            "preview": preview,
            "created_at": self._loop._now_iso(),
        }
        self._set_pending_write(session, pending)
        return CoordinatorToolResult(final_content=format_pending_write_preview(preview))

    async def handle(self, *, msg: InboundMessage, session: Session) -> OutboundMessage | None:
        pending = self._pending_write(session)
        if not pending:
            return None

        command, token, extra_text = extract_pending_write_command(msg)
        pending_token = str(pending.get("token") or "")
        if token and token != pending_token:
            content = "当前没有匹配的待确认写入。请按最新预览直接回复“确认”或“取消”。"
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata={**(msg.metadata or {}), "_tool_turn": True},
            )

        if command == "cancel":
            self._clear_pending_write(session)
            content = "已取消这次待写入操作。"
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata={**(msg.metadata or {}), "_tool_turn": True},
            )

        if command == "confirm" and not extra_text:
            args = dict(pending.get("args") or {})
            tool_name = str(pending.get("tool") or "")
            args["confirm_token"] = pending_token
            result = await self._loop.tools.execute(tool_name, args)
            self._clear_pending_write(session)
            payload = extract_json_object(result)
            content = coerce_pending_write_result(payload) if payload else result
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata={**(msg.metadata or {}), "_tool_turn": True},
            )

        return None
