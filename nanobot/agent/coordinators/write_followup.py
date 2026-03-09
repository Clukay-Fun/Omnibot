"""Coordinator for semantic follow-ups to recent write contexts."""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

from nanobot.agent.coordinators.base import AgentCoordinator
from nanobot.agent.pending_write import coerce_pending_write_result, extract_json_object
from nanobot.agent.tools.registry import ToolExposureContext
from nanobot.agent.write_followup_state import (
    clear_write_contexts,
    get_write_followup_candidates,
    recent_write_contexts,
    set_write_followup_candidates,
)
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.session.manager import Session

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop


class WriteFollowupCoordinator(AgentCoordinator):
    _SELECT_ONE_RE = re.compile(r"^\s*(?:1|第一个|第1个|第1条|第一条)\s*$")
    _SELECT_TWO_RE = re.compile(r"^\s*(?:2|第二个|第2个|第2条|第二条|上一个|前一个|前面那个)\s*$")
    _SELECT_THREE_RE = re.compile(r"^\s*(?:3|第三个|第3个|第3条|第三条)\s*$")

    @property
    def _loop(self) -> AgentLoop:
        assert self._agent is not None
        return self._agent

    @staticmethod
    def _record_direct_turn(session: Session, msg: InboundMessage, assistant_content: str) -> None:
        session.add_message("user", msg.content)
        session.add_message("assistant", assistant_content)

    @classmethod
    def _selection_index(cls, text: str) -> int | None:
        cleaned = text.strip()
        if cls._SELECT_ONE_RE.match(cleaned):
            return 0
        if cls._SELECT_TWO_RE.match(cleaned):
            return 1
        if cls._SELECT_THREE_RE.match(cleaned):
            return 2
        return None

    @staticmethod
    def _render_context_candidates(contexts: list[dict[str, Any]]) -> str:
        lines = ["你最近提到了多条待继续的写入，请直接回复序号选择："]
        for idx, item in enumerate(contexts[:3], start=1):
            table_name = str(item.get("table_name") or "未指定表").strip()
            source_text = str(item.get("source_text") or "").strip()
            preview = source_text[:40] + ("..." if len(source_text) > 40 else "")
            lines.append(f"- {idx}. {table_name}：{preview}")
        return "\n".join(lines)

    async def _interpret_followup(self, *, current_message: str, contexts: list[dict[str, Any]]) -> dict[str, Any]:
        prompt = (
            "你是写入跟进协调器。只输出 JSON，不要调用工具。\n"
            "你的任务是判断用户当前一句话，是否是在继续、修改、取消或切换最近的写入请求。\n"
            "如果不确定，必须输出 confident=false，并尽量 action=ignore。\n"
            "可用 action：execute_previous_write / modify_previous_write / cancel_previous_write / switch_previous_object / ignore\n"
            "字段：action, confident, context_index, merged_request。\n"
            "其中 context_index 从 0 开始；如果无法唯一确定，就返回 null。\n"
            "modify_previous_write 或 switch_previous_object 时，如果能安全重写请求，返回 merged_request；否则给空字符串。\n\n"
            f"最近待继续写入上下文：{json.dumps(contexts, ensure_ascii=False)}\n"
            f"用户当前消息：{current_message}\n\n"
            '只返回 JSON，例如：{"action":"execute_previous_write","confident":true,"context_index":0,"merged_request":""}'
        )
        try:
            response = await asyncio.wait_for(
                self._loop.provider.chat(
                    messages=[
                        {"role": "system", "content": "You are a strict JSON-only write follow-up coordinator."},
                        {"role": "user", "content": prompt},
                    ],
                    tools=None,
                    model=self._loop.model,
                    temperature=0,
                    max_tokens=300,
                    reasoning_effort=None,
                ),
                timeout=min(self._loop._llm_timeout_seconds, self._loop._skillspec_render_primary_timeout_seconds),
            )
        except Exception:
            return {"action": "ignore", "confident": False, "context_index": None, "merged_request": ""}
        payload = extract_json_object(self._loop._strip_think(response.content) or response.content)
        if not isinstance(payload, dict):
            return {"action": "ignore", "confident": False, "context_index": None, "merged_request": ""}
        return {
            "action": str(payload.get("action") or "ignore").strip(),
            "confident": bool(payload.get("confident")),
            "context_index": payload.get("context_index"),
            "merged_request": str(payload.get("merged_request") or "").strip(),
        }

    async def _replay_context(
        self,
        *,
        session: Session,
        msg: InboundMessage,
        context: dict[str, Any],
        interpretation: dict[str, Any],
    ) -> OutboundMessage:
        source_text = str(context.get("source_text") or "").strip()
        table_hint = str(context.get("table_name") or "").strip()
        action = str(interpretation.get("action") or "ignore").strip()
        replay_request = str(interpretation.get("merged_request") or "").strip() or source_text
        if action in {"modify_previous_write", "switch_previous_object"} and not str(interpretation.get("merged_request") or "").strip():
            replay_request = f"{source_text}\n补充：{msg.content.strip()}"

        exposure = ToolExposureContext(channel=msg.channel, user_text=replay_request, mode="main_write_prepare")
        prepare_args: dict[str, Any] = {"request_text": replay_request}
        if table_hint:
            prepare_args["table_hint"] = table_hint

        result = await self._loop.tools.execute("bitable_prepare_create", prepare_args, exposure=exposure)
        prepared_preview = self._loop._capture_coordinator_tool_result(
            session=session,
            tool_name="bitable_prepare_create",
            raw_args=prepare_args,
            result=result,
        )
        metadata = clear_write_contexts(dict(session.metadata or {}))
        session.metadata = metadata
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

    async def handle(self, *, msg: InboundMessage, session: Session) -> OutboundMessage | None:
        pending = session.metadata.get("pending_write")
        if isinstance(pending, dict) and pending:
            return None

        metadata = dict(session.metadata or {})
        candidate_contexts, pending_message = get_write_followup_candidates(metadata)
        selected_index = self._selection_index(msg.content)
        if candidate_contexts and selected_index is not None:
            if selected_index >= len(candidate_contexts):
                content = "没有对应序号的待继续写入，请重新选择。"
                self._record_direct_turn(session, msg, content)
                self._loop.sessions.save(session)
                return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})
            interpretation = await self._interpret_followup(current_message=pending_message or msg.content, contexts=[candidate_contexts[selected_index]])
            return await self._replay_context(session=session, msg=msg, context=candidate_contexts[selected_index], interpretation=interpretation)

        contexts = recent_write_contexts(metadata, now_iso=self._loop._now_iso())
        if not contexts:
            return None

        interpretation = await self._interpret_followup(current_message=msg.content, contexts=contexts)
        action = str(interpretation.get("action") or "ignore").strip()
        confident = bool(interpretation.get("confident"))
        context_index = interpretation.get("context_index")
        if action == "ignore":
            return None
        if action == "cancel_previous_write" and confident:
            session.metadata = clear_write_contexts(metadata)
            content = "已取消最近这条待继续的写入。"
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})
        if not confident:
            if len(contexts) > 1:
                session.metadata = set_write_followup_candidates(metadata, contexts=contexts, current_message=msg.content)
                content = self._render_context_candidates(contexts)
            else:
                content = "我不确定你是在继续刚才那条写入，还是在继续聊天。你可以直接说清要执行哪一条，或把要写入的内容再说完整一点。"
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})

        selected_context: dict[str, Any] | None = None
        if len(contexts) == 1:
            selected_context = contexts[0]
        elif isinstance(context_index, int) and 0 <= context_index < len(contexts):
            selected_context = contexts[context_index]
        else:
            session.metadata = set_write_followup_candidates(metadata, contexts=contexts, current_message=msg.content)
            content = self._render_context_candidates(contexts)
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})

        return await self._replay_context(session=session, msg=msg, context=selected_context, interpretation=interpretation)
