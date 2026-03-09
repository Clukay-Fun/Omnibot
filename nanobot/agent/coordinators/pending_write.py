"""Coordinator for structured write confirmations in normal chat."""

from __future__ import annotations

import asyncio
import json
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
        preview = payload.get("preview") if isinstance(payload.get("preview"), dict) else {}
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
        preview = payload.get("preview") if isinstance(payload.get("preview"), dict) else {}
        if preview.get("table_id"):
            metadata = dict(session.metadata or {})
            previous = metadata.get("recent_selected_table") if isinstance(metadata.get("recent_selected_table"), dict) else {}
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

    async def _load_pending_write_schema(self, pending: dict[str, Any]) -> list[dict[str, Any]]:
        tool_name = str(pending.get("tool") or "")
        if tool_name not in {"bitable_create", "bitable_update"}:
            return []
        if not self._loop.tools.has("bitable_list_fields"):
            return []
        args = pending.get("args") if isinstance(pending.get("args"), dict) else {}
        if not isinstance(args, dict):
            return []
        tool_args: dict[str, Any] = {"compact": True}
        if args.get("app_token"):
            tool_args["app_token"] = args["app_token"]
        if args.get("table_id"):
            tool_args["table_id"] = args["table_id"]
        result = await self._loop.tools.execute("bitable_list_fields", tool_args)
        payload = extract_json_object(result)
        if not payload:
            return []
        fields = payload.get("fields")
        return [item for item in fields if isinstance(item, dict)][:12] if isinstance(fields, list) else []

    async def _interpret_pending_write_reply(self, *, msg: InboundMessage, pending: dict[str, Any]) -> dict[str, Any]:
        schema_fields = await self._load_pending_write_schema(pending)
        prompt = (
            "你是写入确认协调器。只输出 JSON，不要调用工具。\n"
            "根据当前待写入预览和用户最新一句话，判断 action：confirm / cancel / modify / ignore。\n"
            "规则：\n"
            "1. 只有明确确认且没有修改字段时，action 才能是 confirm。\n"
            "2. 用户补充或修改字段时，action=modify，并返回 fields_patch 对象。\n"
            "3. 用户取消时，action=cancel。\n"
            "4. 如果这句话和当前待写入无关，action=ignore。\n"
            "5. fields_patch 只写需要变更的字段；日期优先保留 YYYY-MM-DD，不要自己造毫秒时间戳。\n\n"
            f"当前待写入：{json.dumps(pending.get('preview') or {}, ensure_ascii=False)}\n"
            f"可用字段：{json.dumps(schema_fields, ensure_ascii=False)}\n"
            f"用户消息：{msg.content}\n\n"
            '只返回 JSON，例如：{"action":"modify","fields_patch":{"人员":"房怡康"}}'
        )
        try:
            response = await asyncio.wait_for(
                self._loop.provider.chat(
                    messages=[
                        {"role": "system", "content": "You are a strict JSON-only write confirmation coordinator."},
                        {"role": "user", "content": prompt},
                    ],
                    tools=None,
                    model=self._loop.model,
                    temperature=0,
                    max_tokens=300,
                    reasoning_effort=None,
                ),
                timeout=min(
                    self._loop._llm_timeout_seconds,
                    self._loop._skillspec_render_primary_timeout_seconds,
                ),
            )
        except Exception:
            return {"action": "ignore"}
        payload = extract_json_object(self._loop._strip_think(response.content) or response.content)
        return payload or {"action": "ignore"}

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

        decision = await self._interpret_pending_write_reply(msg=msg, pending=pending)
        action = str(decision.get("action") or "ignore").strip().lower()
        if action == "ignore":
            return None
        if action == "cancel":
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
        if action == "confirm":
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
        if action != "modify":
            return None

        patch = decision.get("fields_patch") if isinstance(decision.get("fields_patch"), dict) else {}
        tool_name = str(pending.get("tool") or "")
        args = dict(pending.get("args") or {})
        fields = dict(args.get("fields") or {}) if isinstance(args.get("fields"), dict) else None
        if not tool_name or fields is None:
            content = "当前待写入操作不支持直接补字段，请先取消后重新发起。"
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata={**(msg.metadata or {}), "_tool_turn": True},
            )

        fields.update(patch)
        args["fields"] = fields
        args.pop("confirm_token", None)
        result = await self._loop.tools.execute(tool_name, args)
        captured = self.on_tool_result(session=session, tool_name=tool_name, raw_args=args, result=result)
        if captured is None:
            payload = extract_json_object(result)
            rendered = coerce_pending_write_result(payload) if payload else result
            self._clear_pending_write(session)
            self._record_direct_turn(session, msg, rendered)
            self._loop.sessions.save(session)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=rendered,
                metadata={**(msg.metadata or {}), "_tool_turn": True},
            )

        refreshed_pending = self._pending_write(session)
        preview = refreshed_pending.get("preview") if isinstance(refreshed_pending.get("preview"), dict) else {}
        rendered = format_pending_write_preview(preview, refreshed=True)
        self._record_direct_turn(session, msg, rendered)
        self._loop.sessions.save(session)
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=rendered,
            metadata={**(msg.metadata or {}), "_tool_turn": True},
        )
