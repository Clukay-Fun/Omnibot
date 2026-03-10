from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nanobot.config.schema import FeishuConfig
from nanobot.feishu.adapter import FeishuAdapter


def _message_payload(*, chat_type: str, chat_id: str = "oc_chat_1", text: str = "hello") -> dict:
    return {
        "header": {
            "event_id": "evt_1",
            "tenant_key": "tenant-1",
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "ou_user_1"},
            },
            "message": {
                "message_id": "om_1",
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": "text",
                "content": '{"text":"%s"}' % text,
            },
        },
    }


@pytest.mark.asyncio
async def test_adapter_uses_dm_session_key_and_strips_mentions() -> None:
    adapter = FeishuAdapter(FeishuConfig())

    translated = await adapter.translate_message(_message_payload(chat_type="p2p", text="@_user_1 hello"))

    assert translated is not None
    assert translated.session_key == "feishu:dm:ou_user_1"
    assert translated.content == "hello"


@pytest.mark.asyncio
async def test_adapter_uses_group_user_session_key_by_default() -> None:
    adapter = FeishuAdapter(FeishuConfig())

    translated = await adapter.translate_message(_message_payload(chat_type="group"))

    assert translated is not None
    assert translated.session_key == "feishu:chat:oc_chat_1:user:ou_user_1"
    assert translated.metadata["tenant_key"] == "tenant-1"


@pytest.mark.asyncio
async def test_adapter_supports_shared_group_mode() -> None:
    adapter = FeishuAdapter(FeishuConfig(group_session_mode="shared"))

    translated = await adapter.translate_message(_message_payload(chat_type="group"))

    assert translated is not None
    assert translated.session_key == "feishu:chat:oc_chat_1"


@pytest.mark.asyncio
async def test_adapter_runs_ttl_check_before_returning_message() -> None:
    ttl_manager = AsyncMock()
    adapter = FeishuAdapter(FeishuConfig(), ttl_manager=ttl_manager)

    translated = await adapter.translate_message(_message_payload(chat_type="p2p", text="hello"))

    assert translated is not None
    ttl_manager.maybe_expire.assert_awaited_once_with(
        "feishu:dm:ou_user_1",
        "tenant-1",
        "ou_user_1",
    )


@pytest.mark.asyncio
async def test_adapter_triggers_overflow_archive_before_translation() -> None:
    overflow_manager = AsyncMock()
    adapter = FeishuAdapter(
        FeishuConfig(),
        overflow_manager=overflow_manager,
        overflow_keep_messages=100,
    )

    translated = await adapter.translate_message(_message_payload(chat_type="p2p", text="hello"))

    assert translated is not None
    overflow_manager.maybe_enqueue_overflow.assert_awaited_once_with(
        "feishu:dm:ou_user_1",
        "tenant-1",
        "ou_user_1",
        keep_messages=100,
    )
