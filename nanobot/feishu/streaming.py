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

    async def handle(self, msg: OutboundMessage) -> bool:
        metadata = msg.metadata or {}
        turn_id = str(metadata.get("turn_id") or "")
        if not turn_id:
            return False

        is_progress = bool(metadata.get("_progress"))
        if not is_progress and turn_id not in self._states:
            return False
        if not self._should_stream(metadata):
            return False

        client = self._client_getter()
        if client is None:
            return False

        payload = self._build_card_payload(msg)
        reply_to = self._reply_target(msg)
        state = self._states.get(turn_id)
        if state is None:
            message_id = await self._create_card(client, msg.chat_id, payload, reply_to=reply_to)
            if not message_id:
                return False
            self._states[turn_id] = _StreamState(
                receive_id_type=FeishuClient.resolve_receive_id_type(msg.chat_id),
                chat_id=msg.chat_id,
                message_id=message_id,
                last_patch_at=self._now(),
            )
            if not is_progress:
                self._states.pop(turn_id, None)
            return True

        if is_progress:
            await self._queue_patch(turn_id, payload)
            return True

        await self._finalize(turn_id, msg, payload)
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

    def _build_card_payload(self, msg: OutboundMessage) -> dict[str, Any]:
        metadata = msg.metadata or {}
        is_progress = bool(metadata.get("_progress"))
        is_tool_hint = bool(metadata.get("_tool_hint"))
        if is_progress and is_tool_hint:
            status = "Working"
            body = f"`{msg.content}`"
            icon = "🔧"
        elif is_progress:
            status = "Thinking"
            body = msg.content
            icon = "⏳"
        else:
            status = "Done"
            body = msg.content or "(empty)"
            icon = "✅"
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"{icon} nanobot · {status}",
                }
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": body,
                }
            ],
        }

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
        if state is None:
            return
        delay = self.throttle_seconds - (self._now() - state.last_patch_at)
        if delay <= 0:
            await self._patch_state(turn_id, payload)
            return
        state.pending_payload = payload
        if state.flush_task is None or state.flush_task.done():
            state.flush_task = asyncio.create_task(self._flush_after(turn_id, delay))

    async def _flush_after(self, turn_id: str, delay: float) -> None:
        try:
            await self._sleep(delay)
            state = self._states.get(turn_id)
            if state is None or state.pending_payload is None:
                return
            payload = state.pending_payload
            state.pending_payload = None
            await self._patch_state(turn_id, payload)
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
        ok = await loop.run_in_executor(
            None,
            client.patch_message_sync,
            state.message_id,
            "interactive",
            json.dumps(payload, ensure_ascii=False),
        )
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
        ok = await self._patch_state(turn_id, payload)
        if not ok:
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
