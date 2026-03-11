from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nanobot.agent.overlay import OverlayContext
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


@pytest.mark.asyncio
async def test_handler_serializes_overlay_context_for_private_chat(tmp_path) -> None:
    publish = AsyncMock()
    adapter = AsyncMock()
    adapter.translate_message = AsyncMock(
        return_value=TranslatedFeishuMessage(
            sender_id="ou_user_1",
            chat_id="ou_user_1",
            content="hello",
            media=[],
            metadata={
                "message_id": "om_1",
                "chat_type": "p2p",
                "tenant_key": "tenant-1",
                "user_open_id": "ou_user_1",
            },
            session_key="feishu:dm:ou_user_1",
        )
    )

    class _PersonaManager:
        def overlay_root_for_chat(self, chat_type, tenant_key, user_open_id):
            assert chat_type == "p2p"
            assert tenant_key == "tenant-1"
            assert user_open_id == "ou_user_1"
            return tmp_path / "users" / "feishu" / "tenant-1" / "ou_user_1"

        def should_include_bootstrap(self, _overlay_root):
            return True

    handler = FeishuEventHandler(adapter=adapter, publish=publish, persona_manager=_PersonaManager())

    envelope = FeishuEnvelope(source="webhook", payload={"header": {"event_id": "evt_1"}, "event": {}})
    await handler.handle_message(envelope)

    published_metadata = publish.await_args.kwargs["metadata"]
    overlay = OverlayContext.from_metadata(published_metadata)
    assert overlay.system_overlay_root == str(tmp_path / "users" / "feishu" / "tenant-1" / "ou_user_1")
    assert overlay.system_overlay_bootstrap is True
