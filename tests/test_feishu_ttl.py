from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.feishu.memory import FeishuUserMemoryStore
from nanobot.feishu.ttl import FeishuTTLManager
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.session.manager import SessionManager


def _make_manager(tmp_path: Path, ttl_seconds: int = 60) -> tuple[FeishuTTLManager, SessionManager, FeishuUserMemoryStore, MagicMock]:
    session_manager = SessionManager(tmp_path)
    memory_store = FeishuUserMemoryStore(tmp_path / "feishu-memory.sqlite3")
    provider = MagicMock()
    manager = FeishuTTLManager(
        session_manager=session_manager,
        memory_store=memory_store,
        provider=provider,
        model="test-model",
        ttl_seconds=ttl_seconds,
    )
    return manager, session_manager, memory_store, provider


@pytest.mark.asyncio
async def test_ttl_archives_and_clears_expired_session(tmp_path: Path) -> None:
    manager, session_manager, memory_store, provider = _make_manager(tmp_path)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call-1",
                    name="save_feishu_user_memory",
                    arguments={
                        "profile": "likes coffee",
                        "summary": "asked about billing",
                    },
                )
            ],
        )
    )

    session = session_manager.get_or_create("feishu:dm:ou_user_1")
    session.add_message("user", "hello")
    session.add_message("assistant", "hi")
    session.updated_at = datetime.now() - timedelta(seconds=120)
    session_manager.save(session)

    expired = await manager.maybe_expire("feishu:dm:ou_user_1", "tenant-1", "ou_user_1")

    assert expired is True
    record = memory_store.get("tenant-1", "ou_user_1")
    assert record is not None
    assert record.profile == "likes coffee"
    assert record.summary == "asked about billing"
    assert session_manager.get_or_create("feishu:dm:ou_user_1").messages == []


@pytest.mark.asyncio
async def test_ttl_skips_recent_session(tmp_path: Path) -> None:
    manager, session_manager, _memory_store, provider = _make_manager(tmp_path)
    provider.chat_with_retry = AsyncMock()

    session = session_manager.get_or_create("feishu:dm:ou_user_1")
    session.add_message("user", "hello")
    session.updated_at = datetime.now()
    session_manager.save(session)

    expired = await manager.maybe_expire("feishu:dm:ou_user_1", "tenant-1", "ou_user_1")

    assert expired is False
    provider.chat_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_ttl_keeps_session_when_archive_fails(tmp_path: Path) -> None:
    manager, session_manager, memory_store, provider = _make_manager(tmp_path)
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="no tool call", tool_calls=[]))

    session = session_manager.get_or_create("feishu:dm:ou_user_1")
    session.add_message("user", "hello")
    session.updated_at = datetime.now() - timedelta(seconds=120)
    session_manager.save(session)

    expired = await manager.maybe_expire("feishu:dm:ou_user_1", "tenant-1", "ou_user_1")

    assert expired is False
    assert memory_store.get("tenant-1", "ou_user_1") is None
    assert len(session_manager.get_or_create("feishu:dm:ou_user_1").messages) == 1


@pytest.mark.asyncio
async def test_ttl_keeps_session_when_archive_payload_is_invalid(tmp_path: Path) -> None:
    manager, session_manager, memory_store, provider = _make_manager(tmp_path)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call-1",
                    name="save_feishu_user_memory",
                    arguments={"profile": "likes coffee", "summary": None},
                )
            ],
        )
    )

    session = session_manager.get_or_create("feishu:dm:ou_user_1")
    session.add_message("user", "hello")
    session.updated_at = datetime.now() - timedelta(seconds=120)
    session_manager.save(session)

    expired = await manager.maybe_expire("feishu:dm:ou_user_1", "tenant-1", "ou_user_1")

    assert expired is False
    assert memory_store.get("tenant-1", "ou_user_1") is None
    assert len(session_manager.get_or_create("feishu:dm:ou_user_1").messages) == 1
