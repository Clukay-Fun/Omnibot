"""Asynchronous event audit sink backed by SQLite."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from nanobot.storage.sqlite_store import SQLiteStore


_SENTINEL = object()


class AuditSink:
    """Queue-based audit writer that flushes events in batches."""

    DEFAULT_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60
    DEFAULT_EVENT_AUDIT_RETENTION_DAYS = 365
    DEFAULT_FEISHU_MESSAGE_INDEX_RETENTION_DAYS = 365

    def __init__(
        self,
        store: SQLiteStore,
        *,
        batch_size: int = 32,
        flush_interval_seconds: float = 0.4,
        queue_maxsize: int = 1000,
        cleanup_interval_seconds: float = DEFAULT_CLEANUP_INTERVAL_SECONDS,
        event_audit_retention_days: int | None = DEFAULT_EVENT_AUDIT_RETENTION_DAYS,
        feishu_message_index_retention_days: int | None = DEFAULT_FEISHU_MESSAGE_INDEX_RETENTION_DAYS,
        enable_cleanup_task: bool = True,
    ) -> None:
        self._store = store
        self._batch_size = max(1, int(batch_size))
        self._flush_interval_seconds = max(0.05, float(flush_interval_seconds))
        self._queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue(maxsize=max(1, int(queue_maxsize)))
        self._cleanup_interval_seconds = max(0.05, float(cleanup_interval_seconds))
        self._event_audit_retention_days = event_audit_retention_days
        self._feishu_message_index_retention_days = feishu_message_index_retention_days
        self._enable_cleanup_task = bool(enable_cleanup_task)
        self._stop_event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._stop_event.clear()
        self._worker_task = asyncio.create_task(self._worker(), name="audit-sink-worker")
        if self._enable_cleanup_task:
            self._cleanup_task = asyncio.create_task(self._cleanup_worker(), name="audit-sink-cleanup-worker")

    async def stop(self) -> None:
        worker_task = self._worker_task
        cleanup_task = self._cleanup_task
        if worker_task is None and cleanup_task is None:
            return
        self._stop_event.set()
        self._worker_task = None
        self._cleanup_task = None
        try:
            self._queue.put_nowait(_SENTINEL)
        except asyncio.QueueFull:
            await self._queue.put(_SENTINEL)
        if worker_task is not None:
            await worker_task
        if cleanup_task is not None:
            await cleanup_task

    def query_event_audit(
        self,
        *,
        event_type: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return self._store.query_event_audit(
            event_type=event_type,
            chat_id=chat_id,
            message_id=message_id,
            start_at=start_at,
            end_at=end_at,
            limit=limit,
            offset=offset,
        )

    def cleanup_event_audit_before(self, before_at: str) -> int:
        return self._store.cleanup_event_audit_before(before_at)

    def cleanup_feishu_message_index_before(self, before_at: str) -> int:
        return self._store.cleanup_feishu_message_index_before(before_at)

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

    def _run_cleanup(self, *, now: datetime | None = None) -> dict[str, int]:
        now_at = now or datetime.now()
        now_iso = now_at.isoformat()
        result = {
            "oauth_states": self._store.cleanup_expired_oauth_states(now_iso=now_iso),
            "event_audit": 0,
            "feishu_message_index": 0,
        }

        if self._event_audit_retention_days is not None and self._event_audit_retention_days >= 0:
            event_cutoff = (now_at - timedelta(days=self._event_audit_retention_days)).isoformat()
            result["event_audit"] = self._store.cleanup_event_audit_before(event_cutoff)

        if self._feishu_message_index_retention_days is not None and self._feishu_message_index_retention_days >= 0:
            index_cutoff = (now_at - timedelta(days=self._feishu_message_index_retention_days)).isoformat()
            result["feishu_message_index"] = self._store.cleanup_feishu_message_index_before(index_cutoff)

        return result

    async def _cleanup_worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                result = self._run_cleanup()
                if any(result.values()):
                    logger.debug(
                        "Audit cleanup completed oauth={} event_audit={} message_index={}",
                        result["oauth_states"],
                        result["event_audit"],
                        result["feishu_message_index"],
                    )
            except Exception:
                logger.exception("Audit cleanup worker failed")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._cleanup_interval_seconds)
            except asyncio.TimeoutError:
                continue
