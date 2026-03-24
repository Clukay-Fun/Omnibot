"""Asynchronous Feishu long-term memory archival helpers."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from loguru import logger

from nanobot.feishu.memory import FeishuMemorySnapshot, FeishuUserMemoryStore
from nanobot.providers.base import LLMProvider
from nanobot.providers.tool_calls import coerce_tool_text, run_required_tool_call
from nanobot.session.manager import Session, SessionManager

FEISHU_ARCHIVED_UNTIL_KEY = "feishu_archived_until"
FEISHU_ARCHIVE_PENDING_UNTIL_KEY = "feishu_archive_pending_until"

_SAVE_FEISHU_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_feishu_user_memory",
            "description": "Save merged Feishu user memory for one tenant user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": "Stable user profile facts to retain long term.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Rolling summary of recent conversation context.",
                    },
                },
                "required": ["profile", "summary"],
            },
        },
    }
]


class FeishuMemoryArchiver:
    """Merge archived Feishu messages into shared long-term memory."""

    def __init__(self, memory_store: FeishuUserMemoryStore, provider: LLMProvider, model: str):
        self.memory_store = memory_store
        self.provider = provider
        self.model = model

    async def archive_messages(
        self,
        tenant_key: str,
        user_open_id: str,
        messages: list[dict[str, Any]],
    ) -> bool:
        lines = []
        for message in messages:
            content = message.get("content")
            if not content:
                continue
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message.get('role', 'user').upper()}: {content}"
            )

        if not lines:
            return True

        existing = self.memory_store.get(tenant_key, user_open_id)
        current_profile = existing.profile if existing else ""
        current_summary = existing.summary if existing else ""
        prompt = f"""Merge this Feishu conversation snapshot into shared user memory and call the save_feishu_user_memory tool.

## Existing Profile
{current_profile or '(empty)'}

## Existing Summary
{current_summary or '(empty)'}

## Conversation To Archive
{chr(10).join(lines)}
"""

        try:
            request_messages = [
                {
                    "role": "system",
                    "content": "You maintain shared Feishu user memory. Call the save_feishu_user_memory tool with merged profile and summary.",
                },
                {"role": "user", "content": prompt},
            ]
            tool_result = await run_required_tool_call(
                self.provider,
                messages=request_messages,
                tools=_SAVE_FEISHU_MEMORY_TOOL,
                tool_name="save_feishu_user_memory",
                required_fields=("profile", "summary"),
                model=self.model,
                temperature=0.0,
                purpose="feishu_archive",
            )
        except Exception:
            logger.exception("Feishu memory archive call failed")
            return False

        if not tool_result.ok and tool_result.error == "missing_tool_call":
            logger.warning("Feishu archive did not return save_feishu_user_memory")
            return False

        if not tool_result.ok and tool_result.error == "invalid_arguments":
            logger.warning("Feishu archive returned unexpected arguments type")
            return False

        if not tool_result.ok and tool_result.error == "missing_required_fields":
            logger.warning(
                "Feishu archive payload missing required fields: {}",
                ", ".join(tool_result.missing_fields),
            )
            return False

        if not tool_result.ok and tool_result.error == "null_required_fields":
            logger.warning(
                "Feishu archive payload contains null required fields: {}",
                ", ".join(tool_result.missing_fields),
            )
            return False

        if not tool_result.ok:
            logger.warning("Feishu archive failed with unexpected tool-call error {}", tool_result.error)
            return False

        arguments = tool_result.arguments or {}
        profile_value = arguments.get("profile")
        summary_value = arguments.get("summary")
        profile = coerce_tool_text(profile_value) if profile_value not in ("", None) else current_profile
        summary = coerce_tool_text(summary_value) if summary_value not in ("", None) else current_summary
        self.memory_store.upsert(
            tenant_key,
            user_open_id,
            profile=profile,
            summary=summary,
        )
        return True


class FeishuAsyncArchiveService:
    """Persist snapshots and merge them into long-term memory in the background."""

    def __init__(
        self,
        *,
        memory_store: FeishuUserMemoryStore,
        session_manager: SessionManager,
        provider: LLMProvider,
        model: str,
    ):
        self.memory_store = memory_store
        self.session_manager = session_manager
        self.archiver = FeishuMemoryArchiver(memory_store, provider, model)
        self._drain_lock = asyncio.Lock()
        self._worker_task: asyncio.Task | None = None

    async def queue_clear_archive(self, session_key: str, tenant_key: str, user_open_id: str) -> bool:
        session = self.session_manager.get_or_create(session_key)
        start_index = self._get_archived_until(session)
        snapshot = self._slice_snapshot(session, start_index, len(session.messages))
        if snapshot:
            self.memory_store.enqueue_snapshot(
                tenant_key=tenant_key,
                user_open_id=user_open_id,
                session_key=session_key,
                reason="clear",
                start_index=start_index,
                end_index=len(session.messages),
                messages=snapshot,
            )
            self._ensure_worker()

        session.clear()
        session.metadata.pop(FEISHU_ARCHIVED_UNTIL_KEY, None)
        session.metadata.pop(FEISHU_ARCHIVE_PENDING_UNTIL_KEY, None)
        self.session_manager.save(session)
        self.session_manager.invalidate(session.key)
        return bool(snapshot)

    async def maybe_enqueue_overflow(
        self,
        session_key: str,
        tenant_key: str,
        user_open_id: str,
        *,
        keep_messages: int,
        start_worker: bool = True,
    ) -> bool:
        if keep_messages <= 0:
            return False

        session = self.session_manager.get_or_create(session_key)
        overflow_end = len(session.messages) - keep_messages
        archived_until = self._get_archived_until(session)
        pending_until = self._get_pending_until(session, archived_until)
        if pending_until > archived_until:
            return False
        if overflow_end <= archived_until:
            return False

        snapshot = self._slice_snapshot(session, archived_until, overflow_end)
        if not snapshot:
            return False

        self.memory_store.enqueue_snapshot(
            tenant_key=tenant_key,
            user_open_id=user_open_id,
            session_key=session_key,
            reason="overflow",
            start_index=archived_until,
            end_index=overflow_end,
            messages=snapshot,
        )
        session.metadata[FEISHU_ARCHIVE_PENDING_UNTIL_KEY] = overflow_end
        session.updated_at = datetime.now()
        self.session_manager.save(session)
        if start_worker:
            self._ensure_worker()
        return True

    async def wait_for_idle(self) -> None:
        while self._worker_task is not None:
            task = self._worker_task
            await task
            if self._worker_task is task:
                self._worker_task = None

    def resume_pending(self) -> None:
        """Resume any persisted pending snapshots after process restart."""
        self.memory_store.reset_running_snapshots()
        if self.memory_store.count_snapshots("pending") > 0:
            self._ensure_worker()

    def kick_worker(self) -> None:
        """Best-effort trigger for draining pending snapshots."""
        if self.memory_store.count_snapshots("pending") > 0:
            self._ensure_worker()

    def _ensure_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._drain_snapshots())

    async def _drain_snapshots(self) -> None:
        async with self._drain_lock:
            while True:
                snapshot = self.memory_store.claim_next_snapshot()
                if snapshot is None:
                    break
                await self._process_snapshot(snapshot)

    async def _process_snapshot(self, snapshot: FeishuMemorySnapshot) -> None:
        success = await self.archiver.archive_messages(
            snapshot.tenant_key,
            snapshot.user_open_id,
            snapshot.messages,
        )
        if success:
            self.memory_store.mark_snapshot_done(snapshot.snapshot_id)
            if snapshot.reason == "overflow":
                self._mark_overflow_success(snapshot.session_key, snapshot.end_index)
            return

        self.memory_store.mark_snapshot_failed(snapshot.snapshot_id, "archive_failed")
        if snapshot.reason == "overflow":
            self._mark_overflow_failure(snapshot.session_key, snapshot.start_index)

    def _mark_overflow_success(self, session_key: str, end_index: int) -> None:
        session = self.session_manager.get_or_create(session_key)
        session.metadata[FEISHU_ARCHIVED_UNTIL_KEY] = max(self._get_archived_until(session), end_index)
        session.metadata[FEISHU_ARCHIVE_PENDING_UNTIL_KEY] = session.metadata[FEISHU_ARCHIVED_UNTIL_KEY]
        self.session_manager.save(session)

    def _mark_overflow_failure(self, session_key: str, fallback_index: int) -> None:
        session = self.session_manager.get_or_create(session_key)
        archived_until = self._get_archived_until(session)
        session.metadata[FEISHU_ARCHIVE_PENDING_UNTIL_KEY] = min(fallback_index, archived_until)
        self.session_manager.save(session)

    @staticmethod
    def _slice_snapshot(session: Session, start_index: int, end_index: int) -> list[dict[str, Any]]:
        if end_index <= start_index:
            return []
        return [dict(message) for message in session.messages[start_index:end_index]]

    @staticmethod
    def _get_archived_until(session: Session) -> int:
        return int(session.metadata.get(FEISHU_ARCHIVED_UNTIL_KEY, 0) or 0)

    @staticmethod
    def _get_pending_until(session: Session, archived_until: int) -> int:
        pending_until = int(session.metadata.get(FEISHU_ARCHIVE_PENDING_UNTIL_KEY, archived_until) or archived_until)
        return max(pending_until, archived_until)
