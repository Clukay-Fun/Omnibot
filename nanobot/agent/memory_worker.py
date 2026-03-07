"""Asynchronous writer for Feishu scoped MEMORY.md files."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from nanobot.utils.helpers import safe_filename

MemoryScope = Literal["user", "chat", "thread"]
_SENTINEL = object()


@dataclass(slots=True)
class MemoryTurnTask:
    channel: str
    user_id: str | None
    chat_id: str | None
    thread_id: str | None
    user_text: str
    assistant_text: str
    message_id: str | None = None
    created_at: str = ""
    scopes: tuple[MemoryScope, ...] = ()


class MemoryWriteWorker:
    """Background queue worker for memory writeback."""

    def __init__(
        self,
        workspace: Path,
        *,
        queue_maxsize: int = 500,
    ) -> None:
        self._workspace = workspace
        self._queue: asyncio.Queue[MemoryTurnTask | object] = asyncio.Queue(maxsize=max(1, queue_maxsize))
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="memory-write-worker")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._task = None
        try:
            self._queue.put_nowait(_SENTINEL)
        except asyncio.QueueFull:
            await self._queue.put(_SENTINEL)
        await task

    async def enqueue(self, task: MemoryTurnTask) -> None:
        if self._task is None or self._task.done():
            await self._write_task(task)
            return
        try:
            self._queue.put_nowait(task)
        except asyncio.QueueFull:
            await self._write_task(task)

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            if item is _SENTINEL:
                break
            if isinstance(item, MemoryTurnTask):
                await self._write_task(item)

        while not self._queue.empty():
            item = await self._queue.get()
            if isinstance(item, MemoryTurnTask):
                await self._write_task(item)

    async def _write_task(self, task: MemoryTurnTask) -> None:
        if task.channel != "feishu":
            return
        entry = self._render_entry(task)
        if not entry:
            return

        for scope in task.scopes:
            path = self._scope_path(scope, task)
            if path is None:
                continue
            self._append_dedup(path, entry)

    def _scope_path(self, scope: MemoryScope, task: MemoryTurnTask) -> Path | None:
        base = self._workspace / "memory" / "feishu"
        if scope == "user" and task.user_id:
            return base / "users" / safe_filename(task.user_id) / "MEMORY.md"
        if scope == "chat" and task.chat_id:
            return base / "chats" / safe_filename(task.chat_id) / "MEMORY.md"
        if scope == "thread" and task.chat_id and task.thread_id:
            key = f"{safe_filename(task.chat_id)}__{safe_filename(task.thread_id)}"
            return base / "threads" / key / "MEMORY.md"
        return None

    def _render_entry(self, task: MemoryTurnTask) -> str:
        user_text = " ".join(task.user_text.split()).strip()
        assistant_text = " ".join(task.assistant_text.split()).strip()
        if not user_text and not assistant_text:
            return ""

        created_at = task.created_at or datetime.now().isoformat()
        dedup_key = self._dedup_key(task, user_text, assistant_text)
        if not dedup_key:
            return ""

        lines = [
            f"<!-- turn:{dedup_key} -->",
            f"- [{created_at[:16].replace('T', ' ')}] U: {user_text[:800]}",
            f"  A: {assistant_text[:1200]}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _dedup_key(task: MemoryTurnTask, user_text: str, assistant_text: str) -> str:
        raw = "|".join(
            [
                task.channel,
                task.user_id or "",
                task.chat_id or "",
                task.thread_id or "",
                task.message_id or "",
                user_text,
                assistant_text,
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _append_dedup(path: Path, entry: str) -> None:
        marker = entry.splitlines()[0].strip()
        existing = ""
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if marker and marker in existing:
                return
        path.parent.mkdir(parents=True, exist_ok=True)
        if existing.strip():
            next_content = f"{existing.rstrip()}\n\n{entry}\n"
        else:
            next_content = f"{entry}\n"
        path.write_text(next_content, encoding="utf-8")
