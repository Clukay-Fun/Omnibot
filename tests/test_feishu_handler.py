from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nanobot.feishu.handler import FeishuEventHandler
from nanobot.feishu.router import FeishuEnvelope
from nanobot.feishu.types import TranslatedFeishuMessage


@pytest.mark.asyncio
async def test_handler_publishes_translated_message() -> None:
    publish = AsyncMock()
    adapter = AsyncMock()
    adapter.translate_message = AsyncMock(
        return_value=TranslatedFeishuMessage(
            sender_id="ou_user_1",
            chat_id="ou_user_1",
            content="hello",
            media=[],
            metadata={"message_id": "om_1"},
            session_key="feishu:dm:ou_user_1",
        )
    )
    handler = FeishuEventHandler(adapter=adapter, publish=publish)

    envelope = FeishuEnvelope(source="webhook", payload={"header": {"event_id": "evt_1"}, "event": {}})
    await handler.handle_message(envelope)

    publish.assert_awaited_once_with(
        sender_id="ou_user_1",
        chat_id="ou_user_1",
        content="hello",
        media=[],
        metadata={"message_id": "om_1"},
        session_key="feishu:dm:ou_user_1",
    )
