"""Feishu card streaming based on stage-level progress events."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.feishu.client import FeishuClient


@dataclass
class _StreamState:
    receive_id_type: str
    chat_id: str
    message_id: str
    last_patch_at: float
    has_progress_patch: bool = False
    degraded: bool = False
    pending_payload: dict[str, Any] | None = None
    flush_task: asyncio.Task | None = None


class FeishuCardStreamer:
    """Render progress/tool-hint/final events into a single updatable Feishu card."""

    def __init__(
        self,
        *,
        client_getter: Callable[[], Any | None],
        scope: str = "dm",
        throttle_seconds: float = 0.5,
        now: Callable[[], float] | None = None,
        sleep: Callable[[float], Any] | None = None,
    ):
        self._client_getter = client_getter
        self.scope = scope
        self.throttle_seconds = throttle_seconds
        self._now = now or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._states: dict[str, _StreamState] = {}
        self._disabled_turns: set[str] = set()

    async def prepare_turn(
        self,
        *,
        turn_id: str,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
        reply_to: str | None = None,
    ) -> bool:
        if not turn_id or turn_id in self._states or turn_id in self._disabled_turns:
            return turn_id in self._states

        meta = metadata or {}
        if not self._should_stream(meta):
            return False

        client = self._client_getter()
        if client is None:
            return False

        message_id = await self._create_card(
            client,
            chat_id,
            self._build_placeholder_payload(),
            reply_to=reply_to,
        )
        if not message_id:
            self._disabled_turns.add(turn_id)
            return False

        self._states[turn_id] = _StreamState(
            receive_id_type=FeishuClient.resolve_receive_id_type(chat_id),
            chat_id=chat_id,
            message_id=message_id,
            last_patch_at=self._now(),
        )
        return True

    async def handle(self, msg: OutboundMessage) -> bool:
        metadata = msg.metadata or {}
        turn_id = str(metadata.get("turn_id") or "")
        if not turn_id:
            return False

        is_progress = bool(metadata.get("_progress"))
        if turn_id in self._disabled_turns:
            if is_progress:
                return True
            self._disabled_turns.discard(turn_id)
            return False
        if not self._should_stream(metadata):
            return False

        client = self._client_getter()
        if client is None:
            return False

        reply_to = self._reply_target(msg)
        state = self._states.get(turn_id)
        if state is None:
            logger.warning(
                "Feishu streaming placeholder not pre-created for turn {}; falling back to on-demand creation",
                turn_id,
            )
            payload = self._build_progress_payload(msg, first=True) if is_progress else self._build_final_payload(msg)
            message_id = await self._create_card(client, msg.chat_id, payload, reply_to=reply_to)
            if not message_id:
                if is_progress:
                    self._disabled_turns.add(turn_id)
                    return True
                return False
            self._states[turn_id] = _StreamState(
                receive_id_type=FeishuClient.resolve_receive_id_type(msg.chat_id),
                chat_id=msg.chat_id,
                message_id=message_id,
                last_patch_at=self._now(),
                has_progress_patch=is_progress,
            )
            if not is_progress:
                self._states.pop(turn_id, None)
            return True

        if state.degraded:
            if is_progress:
                return True
            await self._fallback_send(msg)
            self._states.pop(turn_id, None)
            return True

        if is_progress:
            if not state.has_progress_patch:
                ok = await self._patch_state(turn_id, self._build_progress_payload(msg, first=True))
                if ok:
                    state.has_progress_patch = True
                else:
                    state.degraded = True
                return True

            await self._queue_patch(turn_id, self._build_progress_payload(msg, first=False))
            return True

        await self._finalize(turn_id, msg, self._build_final_payload(msg))
        return True

    async def wait_for_idle(self) -> None:
        while True:
            tasks = [state.flush_task for state in self._states.values() if state.flush_task is not None]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def has_active_stream(self, turn_id: str) -> bool:
        return turn_id in self._states

    def _should_stream(self, metadata: dict[str, Any]) -> bool:
        if self.scope == "off":
            return False
        if self.scope == "all":
            return True
        chat_type = str(metadata.get("chat_type") or "")
        return chat_type != "group"

    def _build_placeholder_payload(self) -> dict[str, Any]:
        return self._build_payload("…")

    def _build_progress_payload(self, msg: OutboundMessage, *, first: bool) -> dict[str, Any]:
        title = "思考中…"
        if not first and bool((msg.metadata or {}).get("_tool_hint")):
            title = self._tool_hint_title(msg.content)
        return self._build_payload("…", title=title)

    def _build_final_payload(self, msg: OutboundMessage) -> dict[str, Any]:
        return self._build_payload(msg.content or "(empty)")

    @staticmethod
    def _build_payload(body: str, *, title: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "markdown",
                    "content": body,
                }
            ],
        }
        if title is not None:
            payload["header"] = {
                "title": {
                    "tag": "plain_text",
                    "content": title,
                }
            }
        return payload

    @staticmethod
    def _tool_hint_title(content: str) -> str:
        if any(name in content for name in ("web_search", "web_fetch")):
            return "正在搜索网络"
        if any(name in content for name in ("read_file", "list_dir")):
            return "正在读取文件"
        if any(name in content for name in ("exec", "spawn")):
            return "正在执行操作"
        return "正在整理结果"

    @staticmethod
    def _reply_target(msg: OutboundMessage) -> str | None:
        if msg.reply_to:
            return msg.reply_to
        metadata = msg.metadata or {}
        reply_to = metadata.get("message_id")
        return str(reply_to) if reply_to else None

    async def _create_card(
        self,
        client: FeishuClient,
        chat_id: str,
        payload: dict[str, Any],
        *,
        reply_to: str | None = None,
    ) -> str | None:
        loop = asyncio.get_running_loop()
        receive_id_type = FeishuClient.resolve_receive_id_type(chat_id)
        return await loop.run_in_executor(
            None,
            client.create_message_sync,
            receive_id_type,
            chat_id,
            "interactive",
            json.dumps(payload, ensure_ascii=False),
            reply_to,
        )

    async def _queue_patch(self, turn_id: str, payload: dict[str, Any]) -> None:
        state = self._states.get(turn_id)
        if state is None or state.degraded:
            return
        delay = self.throttle_seconds - (self._now() - state.last_patch_at)
        if delay <= 0:
            ok = await self._patch_state(turn_id, payload)
            if ok:
                state.has_progress_patch = True
            else:
                state.degraded = True
            return
        state.pending_payload = payload
        if state.flush_task is None or state.flush_task.done():
            state.flush_task = asyncio.create_task(self._flush_after(turn_id, delay))

    async def _flush_after(self, turn_id: str, delay: float) -> None:
        try:
            await self._sleep(delay)
            state = self._states.get(turn_id)
            if state is None or state.degraded or state.pending_payload is None:
                return
            payload = state.pending_payload
            state.pending_payload = None
            ok = await self._patch_state(turn_id, payload)
            if ok:
                state.has_progress_patch = True
            else:
                state.degraded = True
        finally:
            state = self._states.get(turn_id)
            if state is not None:
                state.flush_task = None

    async def _patch_state(self, turn_id: str, payload: dict[str, Any]) -> bool:
        state = self._states.get(turn_id)
        client = self._client_getter()
        if state is None or client is None:
            return False
        loop = asyncio.get_running_loop()
        content = json.dumps(payload, ensure_ascii=False)
        ok = await loop.run_in_executor(None, client.patch_message_sync, state.message_id, "interactive", content)
        if not ok:
            ok = await loop.run_in_executor(None, client.patch_message_sync, state.message_id, "interactive", content)
        if ok:
            state.last_patch_at = self._now()
        else:
            logger.warning("Failed to patch Feishu streaming card {}", state.message_id)
        return bool(ok)

    async def _finalize(self, turn_id: str, msg: OutboundMessage, payload: dict[str, Any]) -> None:
        state = self._states.get(turn_id)
        if state is None:
            return
        if state.flush_task is not None and not state.flush_task.done():
            state.flush_task.cancel()
            try:
                await state.flush_task
            except asyncio.CancelledError:
                pass
            state.flush_task = None
        state.pending_payload = None
        if state.degraded:
            await self._fallback_send(msg)
            self._states.pop(turn_id, None)
            return
        ok = await self._patch_state(turn_id, payload)
        if not ok:
            state.degraded = True
            await self._fallback_send(msg)
        self._states.pop(turn_id, None)

    async def _fallback_send(self, msg: OutboundMessage) -> None:
        client = self._client_getter()
        if client is None:
            return
        loop = asyncio.get_running_loop()
        receive_id_type = FeishuClient.resolve_receive_id_type(msg.chat_id)
        await loop.run_in_executor(
            None,
            client.send_message_sync,
            receive_id_type,
            msg.chat_id,
            "text",
            json.dumps({"text": msg.content}, ensure_ascii=False),
            self._reply_target(msg),
        )
