"""Delayed Feishu progress notices for slow turns."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.feishu.client import FeishuClient
from nanobot.utils.emoji import emojize_text


@dataclass
class _StreamState:
    chat_id: str
    reply_to: str | None
    notice_task: asyncio.Task | None = None
    notice_sent: bool = False


class FeishuCardStreamer:
    """Send a delayed plain-text processing notice for slow Feishu turns."""

    _NOTICE_TEXT = "已收到，正在处理… 🙂"

    def __init__(
        self,
        *,
        client_getter: Callable[[], Any | None],
        scope: str = "dm",
        notice_delay_seconds: float = 2.0,
        sleep: Callable[[float], Any] | None = None,
    ):
        self._client_getter = client_getter
        self.scope = scope
        self.notice_delay_seconds = notice_delay_seconds
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

        if self._client_getter() is None:
            return False

        state = _StreamState(chat_id=chat_id, reply_to=reply_to)
        state.notice_task = asyncio.create_task(self._send_notice_after(turn_id))
        self._states[turn_id] = state
        return True

    async def handle(self, msg: OutboundMessage) -> bool:
        metadata = msg.metadata or {}
        turn_id = str(metadata.get("turn_id") or "")
        is_progress = bool(metadata.get("_progress"))
        if not turn_id or not is_progress:
            return False

        if turn_id in self._disabled_turns:
            return True
        if not self._should_stream(metadata):
            return False
        if self._client_getter() is None:
            return False

        state = self._states.get(turn_id)
        if state is None:
            logger.warning(
                "Feishu delayed notice was not prepared for turn {}; falling back to immediate text notice",
                turn_id,
            )
            await self._send_notice_now(turn_id, msg.chat_id, self._reply_target(msg))
            return True
        return True

    async def cleanup_turn(self, turn_id: str) -> bool:
        """Cancel or retire the delayed notice state for *turn_id*."""
        self._disabled_turns.discard(turn_id)
        state = self._states.pop(turn_id, None)
        if state is None:
            return False
        if state.notice_task is not None and not state.notice_task.done():
            state.notice_task.cancel()
            try:
                await state.notice_task
            except asyncio.CancelledError:
                pass
            state.notice_task = None
        return True

    async def wait_for_idle(self) -> None:
        while True:
            tasks = [state.notice_task for state in self._states.values() if state.notice_task is not None]
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

    @staticmethod
    def _reply_target(msg: OutboundMessage) -> str | None:
        if msg.reply_to:
            return msg.reply_to
        metadata = msg.metadata or {}
        reply_to = metadata.get("message_id")
        return str(reply_to) if reply_to else None

    async def _send_notice_after(self, turn_id: str) -> None:
        try:
            await self._sleep(self.notice_delay_seconds)
            state = self._states.get(turn_id)
            if state is None or state.notice_sent:
                return
            await self._send_notice_now(turn_id, state.chat_id, state.reply_to)
        except asyncio.CancelledError:
            raise
        finally:
            state = self._states.get(turn_id)
            if state is not None:
                state.notice_task = None

    async def _send_notice_now(self, turn_id: str, chat_id: str, reply_to: str | None) -> bool:
        client = self._client_getter()
        if client is None:
            return False
        loop = asyncio.get_running_loop()
        receive_id_type = FeishuClient.resolve_receive_id_type(chat_id)
        ok = await loop.run_in_executor(
            None,
            client.send_message_sync,
            receive_id_type,
            chat_id,
            "text",
            json.dumps({"text": emojize_text(self._NOTICE_TEXT)}, ensure_ascii=False),
            reply_to,
        )
        state = self._states.get(turn_id)
        if ok:
            if state is not None:
                state.notice_sent = True
            return True
        self._disabled_turns.add(turn_id)
        if state is not None:
            self._states.pop(turn_id, None)
        logger.warning("Failed to send delayed Feishu notice for turn {}", turn_id)
        return False
