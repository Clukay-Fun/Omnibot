"""Asynchronous event audit sink backed by SQLite."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from loguru import logger

from nanobot.storage.sqlite_store import SQLiteStore


_SENTINEL = object()


class AuditSink:
    """Queue-based audit writer that flushes events in batches."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        batch_size: int = 32,
        flush_interval_seconds: float = 0.4,
        queue_maxsize: int = 1000,
    ) -> None:
        self._store = store
        self._batch_size = max(1, int(batch_size))
        self._flush_interval_seconds = max(0.05, float(flush_interval_seconds))
        self._queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue(maxsize=max(1, int(queue_maxsize)))
        self._worker_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._worker_task = asyncio.create_task(self._worker(), name="audit-sink-worker")

    async def stop(self) -> None:
        task = self._worker_task
        if task is None:
            return
        self._worker_task = None
        try:
            self._queue.put_nowait(_SENTINEL)
        except asyncio.QueueFull:
            await self._queue.put(_SENTINEL)
        await task

    async def log_event(
        self,
        event_type: str,
        *,
        event_id: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "event_type": event_type,
            "event_id": event_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "payload": payload or {},
            "created_at": datetime.now().isoformat(),
        }
        if self._worker_task is None or self._worker_task.done():
            self._store.record_event_audit_batch([entry])
            return
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            logger.warning("Audit queue is full, writing event directly: {}", event_type)
            self._store.record_event_audit_batch([entry])

    async def _worker(self) -> None:
        batch: list[dict[str, Any]] = []
        stop_requested = False
        while True:
            if stop_requested and self._queue.empty():
                break
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=self._flush_interval_seconds)
            except asyncio.TimeoutError:
                item = None

            if item is _SENTINEL:
                stop_requested = True
            elif isinstance(item, dict):
                batch.append(item)

            if batch and (stop_requested or len(batch) >= self._batch_size or item is None):
                self._store.record_event_audit_batch(batch)
                batch = []

        if batch:
            self._store.record_event_audit_batch(batch)
