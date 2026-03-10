from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.feishu.archive import FeishuAsyncArchiveService
from nanobot.feishu.commands import FeishuCommandHandler
from nanobot.feishu.memory import FeishuUserMemoryStore
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.session.manager import SessionManager
from nanobot.feishu.types import TranslatedFeishuMessage


def _translated(command: str) -> TranslatedFeishuMessage:
    return TranslatedFeishuMessage(
        sender_id="ou_user_1",
        chat_id="ou_user_1",
        content=command,
        metadata={
            "message_id": "om_1",
            "tenant_key": "tenant-1",
            "user_open_id": "ou_user_1",
            "chat_type": "p2p",
        },
        session_key="feishu:dm:ou_user_1",
    )


@pytest.mark.asyncio
async def test_help_command_replies_without_publishing_inbound(tmp_path: Path) -> None:
    respond = AsyncMock()
    handler = FeishuCommandHandler(
        memory_store=FeishuUserMemoryStore(tmp_path / "feishu-memory.sqlite3"),
        respond=respond,
    )
    translated = _translated("/help")

    handled = await handler.handle(translated)

    assert handled is True
    respond.assert_awaited_once()
    outbound = respond.await_args.args[0]
    assert isinstance(outbound, OutboundMessage)
    assert "/clear" in outbound.content
    assert "/forget" in outbound.content
    assert outbound.reply_to == "om_1"


@pytest.mark.asyncio
async def test_clear_command_clears_session_and_archives_in_background(tmp_path: Path) -> None:
    session_manager = SessionManager(tmp_path)
    session = session_manager.get_or_create("feishu:dm:ou_user_1")
    session.add_message("user", "hello")
    session.add_message("assistant", "hi")
    session_manager.save(session)

    provider = MagicMock()
    release = asyncio.Event()

    async def _chat_with_retry(**_kwargs):
        await release.wait()
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call-1",
                    name="save_feishu_user_memory",
                    arguments={
                        "profile": "likes coffee",
                        "summary": "talked about billing",
                    },
                )
            ],
        )

    provider.chat_with_retry = AsyncMock(side_effect=_chat_with_retry)
    store = FeishuUserMemoryStore(tmp_path / "feishu-memory.sqlite3")
    archive_service = FeishuAsyncArchiveService(
        memory_store=store,
        session_manager=session_manager,
        provider=provider,
        model="test-model",
    )
    respond = AsyncMock()
    handler = FeishuCommandHandler(
        memory_store=store,
        respond=respond,
        session_manager=session_manager,
        archive_service=archive_service,
    )
    translated = _translated("/clear")

    handled = await asyncio.wait_for(handler.handle(translated), timeout=0.1)

    assert handled is True
    assert session_manager.get_or_create("feishu:dm:ou_user_1").messages == []
    respond.assert_awaited_once()
    outbound = respond.await_args.args[0]
    assert isinstance(outbound, OutboundMessage)
    assert "cleared" in outbound.content.lower()
    assert outbound.reply_to == "om_1"

    release.set()
    await archive_service.wait_for_idle()
    record = store.get("tenant-1", "ou_user_1")
    assert record is not None
    assert record.summary == "talked about billing"


@pytest.mark.asyncio
async def test_forget_command_clears_user_memory(tmp_path: Path) -> None:
    store = FeishuUserMemoryStore(tmp_path / "feishu-memory.sqlite3")
    store.upsert("tenant-1", "ou_user_1", profile="likes espresso", summary="asked about billing")
    respond = AsyncMock()
    handler = FeishuCommandHandler(memory_store=store, respond=respond)
    translated = _translated("/forget")

    handled = await handler.handle(translated)

    assert handled is True
    assert store.get("tenant-1", "ou_user_1") is None
    respond.assert_awaited_once()
    outbound = respond.await_args.args[0]
    assert "forgot" in outbound.content.lower()
    assert outbound.reply_to == "om_1"
