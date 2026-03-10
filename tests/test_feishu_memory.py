from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nanobot.feishu.handler import FeishuEventHandler
from nanobot.feishu.memory import FeishuUserMemoryStore
from nanobot.feishu.router import FeishuEnvelope
from nanobot.feishu.types import TranslatedFeishuMessage


def test_user_memory_store_round_trip(tmp_path: Path) -> None:
    store = FeishuUserMemoryStore(tmp_path / "feishu-memory.sqlite3")

    store.upsert("tenant-1", "ou_user_1", profile="likes coffee", summary="asked about billing")
    record = store.get("tenant-1", "ou_user_1")

    assert record is not None
    assert record.profile == "likes coffee"
    assert record.summary == "asked about billing"


@pytest.mark.asyncio
async def test_handler_injects_profile_and_summary_for_dm(tmp_path: Path) -> None:
    store = FeishuUserMemoryStore(tmp_path / "feishu-memory.sqlite3")
    store.upsert("tenant-1", "ou_user_1", profile="likes coffee", summary="asked about billing")
    publish = AsyncMock()
    adapter = AsyncMock()
    adapter.translate_message = AsyncMock(
        return_value=TranslatedFeishuMessage(
            sender_id="ou_user_1",
            chat_id="ou_user_1",
            content="hello",
            metadata={
                "tenant_key": "tenant-1",
                "user_open_id": "ou_user_1",
                "chat_type": "p2p",
            },
            session_key="feishu:dm:ou_user_1",
        )
    )
    handler = FeishuEventHandler(adapter=adapter, publish=publish, memory_store=store)

    await handler.handle_message(FeishuEnvelope(source="webhook", payload={}))

    extra_context = publish.await_args.kwargs["metadata"]["extra_context"]
    assert extra_context == ["Profile: likes coffee", "Summary: asked about billing"]


@pytest.mark.asyncio
async def test_handler_injects_only_profile_for_group(tmp_path: Path) -> None:
    store = FeishuUserMemoryStore(tmp_path / "feishu-memory.sqlite3")
    store.upsert("tenant-1", "ou_user_1", profile="likes coffee", summary="asked about billing")
    publish = AsyncMock()
    adapter = AsyncMock()
    adapter.translate_message = AsyncMock(
        return_value=TranslatedFeishuMessage(
            sender_id="ou_user_1",
            chat_id="oc_chat_1",
            content="hello",
            metadata={
                "tenant_key": "tenant-1",
                "user_open_id": "ou_user_1",
                "chat_type": "group",
            },
            session_key="feishu:chat:oc_chat_1:user:ou_user_1",
        )
    )
    handler = FeishuEventHandler(adapter=adapter, publish=publish, memory_store=store)

    await handler.handle_message(FeishuEnvelope(source="webhook", payload={}))

    extra_context = publish.await_args.kwargs["metadata"]["extra_context"]
    assert extra_context == ["Profile: likes coffee"]


@pytest.mark.asyncio
async def test_handler_degrades_when_memory_lookup_fails(tmp_path: Path) -> None:
    store = FeishuUserMemoryStore(tmp_path / "feishu-memory.sqlite3")
    publish = AsyncMock()
    adapter = AsyncMock()
    adapter.translate_message = AsyncMock(
        return_value=TranslatedFeishuMessage(
            sender_id="ou_user_1",
            chat_id="ou_user_1",
            content="hello",
            metadata={
                "tenant_key": "tenant-1",
                "user_open_id": "ou_user_1",
                "chat_type": "p2p",
            },
            session_key="feishu:dm:ou_user_1",
        )
    )

    def boom(*_args, **_kwargs):
        raise RuntimeError("db broken")

    store.get = boom  # type: ignore[method-assign]
    handler = FeishuEventHandler(adapter=adapter, publish=publish, memory_store=store)

    await handler.handle_message(FeishuEnvelope(source="webhook", payload={}))

    assert "extra_context" not in publish.await_args.kwargs["metadata"]
