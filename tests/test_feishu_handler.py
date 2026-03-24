from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.overlay import OverlayContext
from nanobot.feishu.handler import FeishuEventHandler
from nanobot.feishu.memory import FeishuUserMemoryStore
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


@pytest.mark.asyncio
async def test_handler_prepares_placeholder_before_publish() -> None:
    publish = AsyncMock()
    prepare_placeholder = AsyncMock(return_value=True)
    adapter = AsyncMock()
    translated = TranslatedFeishuMessage(
        sender_id="ou_user_1",
        chat_id="ou_user_1",
        content="hello",
        media=[],
        metadata={"message_id": "om_1", "turn_id": "turn_1", "chat_type": "p2p"},
        session_key="feishu:dm:ou_user_1",
    )
    adapter.translate_message = AsyncMock(return_value=translated)

    handler = FeishuEventHandler(
        adapter=adapter,
        publish=publish,
        prepare_placeholder=prepare_placeholder,
    )

    envelope = FeishuEnvelope(source="webhook", payload={"header": {"event_id": "evt_1"}, "event": {}})
    await handler.handle_message(envelope)

    prepare_placeholder.assert_awaited_once_with(translated)
    publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_handler_skips_placeholder_when_command_is_handled() -> None:
    publish = AsyncMock()
    prepare_placeholder = AsyncMock(return_value=True)
    command_handler = AsyncMock()
    command_handler.handle = AsyncMock(return_value=True)
    adapter = AsyncMock()
    translated = TranslatedFeishuMessage(
        sender_id="ou_user_1",
        chat_id="ou_user_1",
        content="/help",
        media=[],
        metadata={"message_id": "om_1", "turn_id": "turn_1", "chat_type": "p2p"},
        session_key="feishu:dm:ou_user_1",
    )
    adapter.translate_message = AsyncMock(return_value=translated)

    handler = FeishuEventHandler(
        adapter=adapter,
        publish=publish,
        command_handler=command_handler,
        prepare_placeholder=prepare_placeholder,
    )

    envelope = FeishuEnvelope(source="webhook", payload={"header": {"event_id": "evt_1"}, "event": {}})
    await handler.handle_message(envelope)

    prepare_placeholder.assert_not_awaited()
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_omits_summary_from_dm_extra_context_when_overlay_exists(tmp_path) -> None:
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
    store = FeishuUserMemoryStore(tmp_path / "feishu-memory.sqlite3")
    store.upsert("tenant-1", "ou_user_1", profile="likes coffee", summary="asked about billing")

    class _PersonaManager:
        def overlay_root_for_chat(self, chat_type, tenant_key, user_open_id):
            assert chat_type == "p2p"
            return tmp_path / "users" / "feishu" / tenant_key / user_open_id

        def should_include_bootstrap(self, _overlay_root):
            return True

    handler = FeishuEventHandler(
        adapter=adapter,
        publish=publish,
        memory_store=store,
        persona_manager=_PersonaManager(),
    )

    envelope = FeishuEnvelope(source="webhook", payload={"header": {"event_id": "evt_1"}, "event": {}})
    await handler.handle_message(envelope)

    assert publish.await_args.kwargs["metadata"]["extra_context"] == ["Profile: likes coffee"]


@pytest.mark.asyncio
async def test_handler_logs_bootstrap_decision_with_status(tmp_path) -> None:
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
        def overlay_root_for_chat(self, _chat_type, tenant_key, user_open_id):
            return tmp_path / "users" / "feishu" / tenant_key / user_open_id

        def bootstrap_status(self, _overlay_root):
            return {
                "include_bootstrap": False,
                "has_name": True,
                "has_style": True,
                "has_long_term_context": True,
                "has_current_work": True,
            }

    with patch("nanobot.feishu.handler.logger") as mock_logger:
        mock_logger.bind.return_value = MagicMock()
        handler = FeishuEventHandler(adapter=adapter, publish=publish, persona_manager=_PersonaManager())
        envelope = FeishuEnvelope(source="webhook", payload={"header": {"event_id": "evt_1"}, "event": {}})
        await handler.handle_message(envelope)

    assert any(
        call.kwargs.get("event") == "feishu_bootstrap_decision"
        and call.kwargs.get("include_bootstrap") is False
        and call.kwargs.get("has_current_work") is True
        for call in mock_logger.bind.call_args_list
    )
