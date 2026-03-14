"""Feishu thinking-card progress streaming."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Callable

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.feishu.client import FeishuClient
from nanobot.feishu.thinking_card import (
    build_completed,
    build_initial,
    build_minimal,
    build_progress,
)


@dataclass
class _StreamState:
    message_id: str
    chat_id: str
    reply_to: str | None
    entries: list[str] = field(default_factory=list)
    has_meaningful_entry: bool = False
    completed: bool = False
    pending_patch: bool = False
    flush_task: asyncio.Task | None = None
    last_patch_at: float | None = None


@dataclass
class _PreparedTurn:
    chat_id: str
    reply_to: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


class FeishuCardStreamer:
    """Create and update Feishu interactive thinking cards for slow turns."""

    _INITIAL_ENTRY = "思考中…"
    _MAX_ENTRY_CHARS = 80

    def __init__(
        self,
        *,
        client_getter: Callable[[], Any | None],
        scope: str = "dm",
        throttle_seconds: float = 0.5,
        sleep: Callable[[float], Any] | None = None,
    ):
        self._client_getter = client_getter
        self.scope = scope
        self.throttle_seconds = throttle_seconds
        self._sleep = sleep or asyncio.sleep
        self._states: dict[str, _StreamState] = {}
        self._prepared_turns: dict[str, _PreparedTurn] = {}
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
            return turn_id in self._states or turn_id in self._prepared_turns

        meta = metadata or {}
        if not self._should_stream(meta):
            return False

        self._prepared_turns[turn_id] = _PreparedTurn(
            chat_id=chat_id,
            reply_to=reply_to,
            metadata=dict(meta),
        )
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

        is_tool_progress = bool(
            metadata.get("_is_tool_progress")
            if "_is_tool_progress" in metadata
            else metadata.get("_tool_hint")
        )

        state = self._states.get(turn_id)
        if state is None:
            if not is_tool_progress:
                return True
            state = await self._ensure_state(
                turn_id=turn_id,
                chat_id=msg.chat_id,
                metadata=metadata,
                reply_to=self._reply_target(msg),
            )
            if state is None:
                return True

        updated = self._append_progress_entry(
            state,
            msg.content,
            tool_hint=bool(metadata.get("_tool_hint")),
        )
        if not updated:
            return True

        if state.last_patch_at is None:
            await self._patch_state(turn_id, state)
            return True

        await self._schedule_flush(turn_id, state)
        return True

    async def complete_turn(self, turn_id: str) -> bool:
        self._disabled_turns.discard(turn_id)
        self._prepared_turns.pop(turn_id, None)
        state = self._states.pop(turn_id, None)
        if state is None:
            return False

        await self._cancel_flush(state)

        if state.completed:
            return True

        client = self._client_getter()
        if client is None:
            return False

        payload = build_completed(state.entries) if state.entries else build_minimal()
        ok = await self._patch_message(client, state.message_id, payload)
        if ok:
            state.completed = True
        return ok

    async def cleanup_turn(self, turn_id: str) -> bool:
        """Drop local turn state without marking completion."""
        self._disabled_turns.discard(turn_id)
        prepared = self._prepared_turns.pop(turn_id, None)
        state = self._states.pop(turn_id, None)
        if state is None:
            return prepared is not None
        await self._cancel_flush(state)
        return True

    async def wait_for_idle(self) -> None:
        while True:
            tasks = [state.flush_task for state in self._states.values() if state.flush_task is not None]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def has_active_stream(self, turn_id: str) -> bool:
        return turn_id in self._states

    async def _ensure_state(
        self,
        *,
        turn_id: str,
        chat_id: str,
        metadata: dict[str, Any],
        reply_to: str | None,
    ) -> _StreamState | None:
        state = self._states.get(turn_id)
        if state is not None:
            return state

        prepared = self._prepared_turns.get(turn_id)
        if prepared is None:
            logger.warning(
                "Feishu thinking card was not prepared for turn {}; falling back to on-demand creation",
                turn_id,
            )
            prepared = _PreparedTurn(
                chat_id=chat_id,
                reply_to=reply_to,
                metadata=dict(metadata),
            )
            self._prepared_turns[turn_id] = prepared

        client = self._client_getter()
        if client is None:
            self._prepared_turns.pop(turn_id, None)
            self._disabled_turns.add(turn_id)
            return None

        message_id = await self._create_card(client, prepared.chat_id, prepared.reply_to, build_initial())
        if not message_id:
            self._prepared_turns.pop(turn_id, None)
            self._disabled_turns.add(turn_id)
            return None

        state = _StreamState(
            message_id=message_id,
            chat_id=prepared.chat_id,
            reply_to=prepared.reply_to,
        )
        self._states[turn_id] = state
        self._prepared_turns.pop(turn_id, None)
        return state

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

    async def _create_card(
        self,
        client: Any,
        chat_id: str,
        reply_to: str | None,
        payload: dict[str, Any],
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

    async def _patch_message(self, client: Any, message_id: str, payload: dict[str, Any]) -> bool:
        loop = asyncio.get_running_loop()
        return bool(
            await loop.run_in_executor(
                None,
                client.patch_message_sync,
                message_id,
                "interactive",
                json.dumps(payload, ensure_ascii=False),
            )
        )

    async def _patch_state(self, turn_id: str, state: _StreamState) -> bool:
        client = self._client_getter()
        if client is None:
            self._states.pop(turn_id, None)
            self._disabled_turns.add(turn_id)
            return False

        payload = build_progress(state.entries)
        logger.debug("Feishu thinking card patch for turn {}: {}", turn_id, state.entries)
        ok = await self._patch_message(client, state.message_id, payload)
        if ok:
            state.pending_patch = False
            state.last_patch_at = time.monotonic()
            return True

        self._states.pop(turn_id, None)
        self._disabled_turns.add(turn_id)
        logger.warning("Failed to patch Feishu thinking card for turn {}", turn_id)
        return False

    async def _schedule_flush(self, turn_id: str, state: _StreamState) -> None:
        state.pending_patch = True
        if state.flush_task is not None and not state.flush_task.done():
            return

        delay = max(
            0.0,
            self.throttle_seconds - max(0.0, time.monotonic() - (state.last_patch_at or 0.0)),
        )

        async def _flush() -> None:
            try:
                if delay > 0:
                    await self._sleep(delay)
                current = self._states.get(turn_id)
                if current is None or not current.pending_patch or current.completed:
                    return
                await self._patch_state(turn_id, current)
            except asyncio.CancelledError:
                raise
            finally:
                current = self._states.get(turn_id)
                if current is not None:
                    current.flush_task = None

        state.flush_task = asyncio.create_task(_flush())

    async def _cancel_flush(self, state: _StreamState) -> None:
        task = state.flush_task
        if task is None or task.done():
            state.flush_task = None
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        state.flush_task = None

    def _append_progress_entry(self, state: _StreamState, content: str, *, tool_hint: bool) -> bool:
        if not state.entries:
            state.entries.append(self._INITIAL_ENTRY)

        rendered = self._render_entry(content, tool_hint=tool_hint)
        if not rendered:
            return False
        if state.entries and state.entries[-1] == rendered:
            return False

        state.entries.append(rendered)
        if tool_hint or rendered not in {self._INITIAL_ENTRY, "", "…"}:
            state.has_meaningful_entry = True
        return True

    def _render_entry(self, content: str, *, tool_hint: bool) -> str | None:
        text = " ".join((content or "").strip().split())
        if not text:
            return None
        if tool_hint:
            return self._render_tool_hint(text)
        return self._clip_entry(text)

    def _render_tool_hint(self, hint: str) -> str:
        name, arg = self._parse_tool_hint(hint)
        specialized = self._render_specialized_tool_hint(name, arg)
        if specialized:
            return specialized
        if name in {"web_search", "web_fetch"}:
            prefix = "正在搜索网络"
        elif name in {"read_file", "list_dir"}:
            prefix = "正在读取文件"
        elif name in {"exec", "spawn"}:
            prefix = "正在执行操作"
        else:
            prefix = "正在处理"

        if arg:
            return self._clip_entry(f"{prefix}：{arg}")
        if name and name != hint:
            return self._clip_entry(f"{prefix}：{name}")
        return self._clip_entry(f"{prefix}：{hint}")

    def _render_specialized_tool_hint(self, name: str, arg: str) -> str | None:
        if name == "read_file" and arg:
            rendered = self._render_skill_file_hint(arg)
            if rendered:
                return rendered
        if name == "list_dir" and arg:
            rendered = self._render_skill_dir_hint(arg)
            if rendered:
                return rendered
        if name == "exec" and arg:
            rendered = self._render_skill_exec_hint(arg)
            if rendered:
                return rendered
        return None

    def _render_skill_file_hint(self, path: str) -> str | None:
        normalized = path.strip()
        if "nanobot/skills/feishu-workspace" not in normalized:
            return None
        if normalized.endswith("/README.md") or normalized.endswith("/SKILL.md"):
            return "正在读取飞书工作台 skill 说明"
        if normalized.endswith("/references/bitable.md"):
            return "正在读取多维表格能力说明"
        if normalized.endswith("/references/calendar.md"):
            return "正在读取飞书日历能力说明"
        if normalized.endswith("/references/docs.md"):
            return "正在读取飞书文档能力说明"
        filename = PurePath(normalized).name
        if filename:
            return self._clip_entry(f"正在读取 skill 文件：{filename}")
        return None

    def _render_skill_dir_hint(self, path: str) -> str | None:
        normalized = path.strip()
        if "nanobot/skills/feishu-workspace" not in normalized:
            return None
        if normalized.endswith("/scripts"):
            return "正在查看飞书工作台 skill 脚本目录"
        return "正在查看飞书工作台 skill 目录"

    def _render_skill_exec_hint(self, command: str) -> str | None:
        compact = " ".join(command.split())
        if "nanobot/skills/feishu-workspace/scripts/" not in compact:
            return None

        if "/bitable.sh check" in compact:
            return "正在检查多维表格能力"
        if "/bitable.sh table list" in compact:
            return "正在列出多维表格"
        if "/bitable.sh view list" in compact:
            return "正在列出多维表格视图"
        if "/bitable.sh record list" in compact:
            return "正在读取多维表格记录"
        if "/bitable.sh record get" in compact:
            return "正在读取多维表格记录详情"
        if "/calendar.sh check" in compact:
            return "正在检查飞书日历能力"
        if "/calendar.sh event list" in compact:
            return "正在列出日历事件"
        if "/calendar.sh calendar list" in compact:
            return "正在列出可访问日历"
        if "/docs.sh check" in compact:
            return "正在检查飞书文档能力"
        if "/docs.sh doc read_text" in compact or "/docs.sh doc get" in compact:
            return "正在读取飞书文档内容"
        if "/docs.sh drive" in compact:
            return "正在检查云空间文件"

        script_name = ""
        if "/scripts/" in compact:
            script_name = compact.split("/scripts/", 1)[1].split(" ", 1)[0]
        if script_name:
            return self._clip_entry(f"正在执行 skill：{script_name}")
        return "正在执行飞书工作台 skill"

    @classmethod
    def _parse_tool_hint(cls, hint: str) -> tuple[str, str]:
        if "(" not in hint or not hint.endswith(")") and "\", " not in hint:
            return hint, ""
        head = hint.split(",", 1)[0].strip()
        if "(" not in head:
            return head, ""
        name, rest = head.split("(", 1)
        arg = rest.rsplit(")", 1)[0].strip()
        if arg.startswith('"') and arg.endswith('"'):
            arg = arg[1:-1]
        return name.strip(), arg

    @classmethod
    def _clip_entry(cls, text: str) -> str:
        if len(text) <= cls._MAX_ENTRY_CHARS:
            return text
        return text[: cls._MAX_ENTRY_CHARS - 1] + "…"
