"""Lazy TTL expiry for Feishu short-term sessions."""

from __future__ import annotations

from datetime import datetime, timedelta

from loguru import logger

from nanobot.feishu.archive import (
    FEISHU_ARCHIVE_PENDING_UNTIL_KEY,
    FEISHU_ARCHIVED_UNTIL_KEY,
    FeishuMemoryArchiver,
)
from nanobot.feishu.memory import FeishuUserMemoryStore
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import SessionManager


class FeishuTTLManager:
    """Synchronously archive and clear expired Feishu short-term sessions."""

    def __init__(
        self,
        *,
        session_manager: SessionManager,
        memory_store: FeishuUserMemoryStore,
        provider: LLMProvider,
        model: str,
        ttl_seconds: int,
    ):
        self.session_manager = session_manager
        self.memory_store = memory_store
        self.provider = provider
        self.model = model
        self.ttl_seconds = ttl_seconds
        self.archiver = FeishuMemoryArchiver(memory_store, provider, model)

    async def maybe_expire(self, session_key: str, tenant_key: str, user_open_id: str) -> bool:
        """Archive and clear the session if it has expired."""
        if self.ttl_seconds <= 0 or not session_key or not tenant_key or not user_open_id:
            return False

        session = self.session_manager.get_or_create(session_key)
        if not session.messages:
            return False

        expires_before = datetime.now() - timedelta(seconds=self.ttl_seconds)
        if session.updated_at >= expires_before:
            return False

        start_index = int(session.metadata.get(FEISHU_ARCHIVED_UNTIL_KEY, 0) or 0)
        archived = await self.archiver.archive_messages(
            tenant_key,
            user_open_id,
            [dict(message) for message in session.messages[start_index:]],
        )
        if not archived:
            logger.warning("Feishu TTL archive failed for {}, keeping session", session_key)
            return False

        session.clear()
        session.metadata.pop(FEISHU_ARCHIVED_UNTIL_KEY, None)
        session.metadata.pop(FEISHU_ARCHIVE_PENDING_UNTIL_KEY, None)
        self.session_manager.save(session)
        self.session_manager.invalidate(session.key)
        return True
