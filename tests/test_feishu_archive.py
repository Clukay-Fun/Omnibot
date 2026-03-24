from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.feishu.archive import FEISHU_ARCHIVED_UNTIL_KEY, FeishuAsyncArchiveService, FeishuMemoryArchiver
from nanobot.feishu.memory import FeishuUserMemoryStore
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.session.manager import SessionManager


def _make_archive_service(tmp_path: Path) -> tuple[FeishuAsyncArchiveService, SessionManager, FeishuUserMemoryStore, MagicMock]:
    session_manager = SessionManager(tmp_path)
    memory_store = FeishuUserMemoryStore(tmp_path / "feishu-memory.sqlite3")
    provider = MagicMock()
    service = FeishuAsyncArchiveService(
        memory_store=memory_store,
        session_manager=session_manager,
        provider=provider,
        model="test-model",
    )
    return service, session_manager, memory_store, provider


def _make_archiver(tmp_path: Path) -> tuple[FeishuMemoryArchiver, FeishuUserMemoryStore, MagicMock]:
    memory_store = FeishuUserMemoryStore(tmp_path / "feishu-memory.sqlite3")
    provider = MagicMock()
    archiver = FeishuMemoryArchiver(memory_store, provider, "test-model")
    return archiver, memory_store, provider


@pytest.mark.asyncio
async def test_overflow_archive_persists_summary_and_updates_cursor(tmp_path: Path) -> None:
    service, session_manager, memory_store, provider = _make_archive_service(tmp_path)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call-1",
                    name="save_feishu_user_memory",
                    arguments={
                        "profile": "likes coffee",
                        "summary": "older messages archived",
                    },
                )
            ],
        )
    )

    session = session_manager.get_or_create("feishu:dm:ou_user_1")
    for i in range(8):
        session.add_message("user" if i % 2 == 0 else "assistant", f"msg-{i}")
    session_manager.save(session)

    queued = await service.maybe_enqueue_overflow(
        "feishu:dm:ou_user_1",
        "tenant-1",
        "ou_user_1",
        keep_messages=4,
    )

    assert queued is True
    await service.wait_for_idle()

    record = memory_store.get("tenant-1", "ou_user_1")
    assert record is not None
    assert record.summary == "older messages archived"

    session_after = session_manager.get_or_create("feishu:dm:ou_user_1")
    assert len(session_after.messages) == 8
    assert session_after.metadata[FEISHU_ARCHIVED_UNTIL_KEY] == 4


@pytest.mark.asyncio
async def test_overflow_archive_waits_for_next_message_before_requeue(tmp_path: Path) -> None:
    service, session_manager, _memory_store, provider = _make_archive_service(tmp_path)

    release = AsyncMock()

    async def _chat_with_retry(**_kwargs):
        await release()
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call-1",
                    name="save_feishu_user_memory",
                    arguments={"profile": "p", "summary": "s"},
                )
            ],
        )

    provider.chat_with_retry = AsyncMock(side_effect=_chat_with_retry)

    session = session_manager.get_or_create("feishu:dm:ou_user_1")
    for i in range(8):
        session.add_message("user" if i % 2 == 0 else "assistant", f"msg-{i}")
    session_manager.save(session)

    first = await service.maybe_enqueue_overflow("feishu:dm:ou_user_1", "tenant-1", "ou_user_1", keep_messages=4)
    second = await service.maybe_enqueue_overflow("feishu:dm:ou_user_1", "tenant-1", "ou_user_1", keep_messages=4)

    assert first is True
    assert second is False
    await service.wait_for_idle()


@pytest.mark.asyncio
async def test_overflow_archive_can_delay_worker_until_kicked(tmp_path: Path) -> None:
    service, session_manager, memory_store, provider = _make_archive_service(tmp_path)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call-1",
                    name="save_feishu_user_memory",
                    arguments={"profile": "likes tea", "summary": "archived later"},
                )
            ],
        )
    )

    session = session_manager.get_or_create("feishu:dm:ou_user_1")
    for i in range(8):
        session.add_message("user" if i % 2 == 0 else "assistant", f"msg-{i}")
    session_manager.save(session)

    queued = await service.maybe_enqueue_overflow(
        "feishu:dm:ou_user_1",
        "tenant-1",
        "ou_user_1",
        keep_messages=4,
        start_worker=False,
    )

    assert queued is True
    provider.chat_with_retry.assert_not_awaited()
    assert memory_store.count_snapshots("pending") == 1

    service.kick_worker()
    await service.wait_for_idle()

    provider.chat_with_retry.assert_awaited_once()
    record = memory_store.get("tenant-1", "ou_user_1")
    assert record is not None
    assert record.summary == "archived later"


@pytest.mark.asyncio
async def test_archiver_accepts_json_string_arguments(tmp_path: Path) -> None:
    archiver, memory_store, provider = _make_archiver(tmp_path)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call-1",
                    name="save_feishu_user_memory",
                    arguments=json.dumps({
                        "profile": "likes coffee",
                        "summary": "asked about billing",
                    }),
                )
            ],
        )
    )

    archived = await archiver.archive_messages(
        "tenant-1",
        "ou_user_1",
        [{"role": "user", "content": "hello", "timestamp": "2026-01-01 00:00"}],
    )

    assert archived is True
    record = memory_store.get("tenant-1", "ou_user_1")
    assert record is not None
    assert record.profile == "likes coffee"
    assert record.summary == "asked about billing"


@pytest.mark.asyncio
async def test_archiver_accepts_list_wrapped_dict_arguments(tmp_path: Path) -> None:
    archiver, memory_store, provider = _make_archiver(tmp_path)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call-1",
                    name="save_feishu_user_memory",
                    arguments=[{
                        "profile": "likes tea",
                        "summary": "prefers async updates",
                    }],
                )
            ],
        )
    )

    archived = await archiver.archive_messages(
        "tenant-1",
        "ou_user_1",
        [{"role": "user", "content": "hello", "timestamp": "2026-01-01 00:00"}],
    )

    assert archived is True
    record = memory_store.get("tenant-1", "ou_user_1")
    assert record is not None
    assert record.profile == "likes tea"
    assert record.summary == "prefers async updates"


@pytest.mark.asyncio
async def test_archiver_falls_back_to_auto_tool_choice_when_forced_call_is_unsupported(tmp_path: Path) -> None:
    archiver, memory_store, provider = _make_archiver(tmp_path)
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(
                content='Provider says tool_choice should be ["none", "auto"]',
                finish_reason="error",
            ),
            LLMResponse(
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
            ),
        ]
    )

    archived = await archiver.archive_messages(
        "tenant-1",
        "ou_user_1",
        [{"role": "user", "content": "hello", "timestamp": "2026-01-01 00:00"}],
    )

    assert archived is True
    assert provider.chat_with_retry.await_count == 2
    first_call = provider.chat_with_retry.await_args_list[0].kwargs
    second_call = provider.chat_with_retry.await_args_list[1].kwargs
    assert first_call["tool_choice"]["function"]["name"] == "save_feishu_user_memory"
    assert second_call["tool_choice"] == "auto"
    record = memory_store.get("tenant-1", "ou_user_1")
    assert record is not None
    assert record.summary == "asked about billing"


@pytest.mark.asyncio
async def test_overflow_archive_does_not_advance_cursor_when_payload_is_invalid(tmp_path: Path) -> None:
    service, session_manager, memory_store, provider = _make_archive_service(tmp_path)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call-1",
                    name="save_feishu_user_memory",
                    arguments={"profile": "likes coffee"},
                )
            ],
        )
    )

    session = session_manager.get_or_create("feishu:dm:ou_user_1")
    for i in range(8):
        session.add_message("user" if i % 2 == 0 else "assistant", f"msg-{i}")
    session_manager.save(session)

    queued = await service.maybe_enqueue_overflow(
        "feishu:dm:ou_user_1",
        "tenant-1",
        "ou_user_1",
        keep_messages=4,
    )

    assert queued is True
    await service.wait_for_idle()

    session_after = session_manager.get_or_create("feishu:dm:ou_user_1")
    assert int(session_after.metadata.get(FEISHU_ARCHIVED_UNTIL_KEY, 0) or 0) == 0
    assert memory_store.count_snapshots("failed") == 1
