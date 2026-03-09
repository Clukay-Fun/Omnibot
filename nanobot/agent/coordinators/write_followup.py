"""Coordinator for affirmative follow-ups to recent write promises without pending preview state."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from nanobot.agent.coordinators.base import AgentCoordinator
from nanobot.agent.pending_write import coerce_pending_write_result, extract_json_object
from nanobot.agent.tools.registry import ToolExposureContext
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.session.manager import Session

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop


class WriteFollowupCoordinator(AgentCoordinator):
    _AFFIRMATIVE_TOKENS = (
        "需要",
        "可以",
        "确认",
        "就这些填进去",
        "填进去",
        "录进去",
        "就按这个",
        "按这个",
        "创建吧",
        "更新吧",
        "开始吧",
        "好",
        "好的",
        "行",
    )
    _WRITE_VERBS = ("新增", "创建", "录入", "写入", "更新", "修改", "补", "填")
    _PROMISE_TOKENS = ("确认后", "成功后", "我会", "我现在就", "我来按", "回你")

    @property
    def _loop(self) -> AgentLoop:
        assert self._agent is not None
        return self._agent

    @staticmethod
    def _record_direct_turn(session: Session, msg: InboundMessage, assistant_content: str) -> None:
        session.add_message("user", msg.content)
        session.add_message("assistant", assistant_content)

    @classmethod
    def _is_affirmative_followup(cls, text: str) -> bool:
        cleaned = text.strip()
        if not cleaned:
            return False
        return any(token in cleaned for token in cls._AFFIRMATIVE_TOKENS)

    @classmethod
    def _looks_like_write_request(cls, text: str) -> bool:
        cleaned = text.strip()
        return bool(cleaned) and any(token in cleaned for token in cls._WRITE_VERBS)

    @classmethod
    def _looks_like_write_promise(cls, text: str) -> bool:
        cleaned = text.strip()
        return bool(cleaned) and any(token in cleaned for token in cls._PROMISE_TOKENS) and any(
            token in cleaned for token in ("创建", "更新", "录入", "写入", "记录链接", "字段")
        )

    @staticmethod
    def _recent_user_and_assistant(session: Session) -> tuple[str, str]:
        user_text = ""
        assistant_text = ""
        messages = session.messages[-8:]
        for item in reversed(messages):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            content = item.get("content")
            if role == "assistant" and not assistant_text and isinstance(content, str):
                assistant_text = content.strip()
                continue
            if role == "user" and assistant_text and isinstance(content, str):
                user_text = content.strip()
                break
        return user_text, assistant_text

    async def handle(self, *, msg: InboundMessage, session: Session) -> OutboundMessage | None:
        if not self._is_affirmative_followup(msg.content):
            return None
        pending = session.metadata.get("pending_write")
        if isinstance(pending, dict) and pending:
            return None

        source_text, assistant_text = self._recent_user_and_assistant(session)
        if not self._looks_like_write_promise(assistant_text):
            return None
        if not self._looks_like_write_request(source_text):
            content = "我还没有生成可执行的写入预览，请把要写入的内容再明确发我一次。"
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})

        selected_table = session.metadata.get("recent_selected_table") if isinstance(session.metadata.get("recent_selected_table"), dict) else {}
        table_hint = str(selected_table.get("table_name") or selected_table.get("name") or "").strip()
        exposure = ToolExposureContext(channel=msg.channel, user_text=source_text, mode="main_write_prepare")
        prepare_args: dict[str, Any] = {"request_text": source_text}
        if table_hint:
            prepare_args["table_hint"] = table_hint

        result = await self._loop.tools.execute("bitable_prepare_create", prepare_args, exposure=exposure)
        prepared_preview = self._loop._capture_coordinator_tool_result(
            session=session,
            tool_name="bitable_prepare_create",
            raw_args=prepare_args,
            result=result,
        )
        if prepared_preview is not None:
            content = prepared_preview
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})

        followup = self._loop._prepared_write_followup("bitable_prepare_create", result)
        if followup is None:
            payload = extract_json_object(result)
            content = coerce_pending_write_result(payload) if payload else result
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})

        followup_tool = str(followup.get("tool") or "").strip()
        followup_args = dict(followup.get("arguments") or {})
        followup_result = await self._loop.tools.execute(followup_tool, followup_args, exposure=exposure)
        content = self._loop._capture_coordinator_tool_result(
            session=session,
            tool_name=followup_tool,
            raw_args=followup_args,
            result=followup_result,
        )
        if content is None:
            payload = extract_json_object(followup_result)
            content = coerce_pending_write_result(payload) if payload else followup_result
        self._record_direct_turn(session, msg, content)
        self._loop.sessions.save(session)
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})
