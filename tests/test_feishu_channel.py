from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.channels.feishu import FeishuChannel


def _build_channel() -> FeishuChannel:
    channel = object.__new__(FeishuChannel)
    channel._streaming = AsyncMock()
    channel._outbound = AsyncMock()
    channel._archive_service = MagicMock()
    return channel


@pytest.mark.asyncio
async def test_feishu_channel_routes_progress_to_streamer() -> None:
    channel = _build_channel()
    channel._streaming.handle = AsyncMock(return_value=True)
    msg = OutboundMessage(channel="feishu", chat_id="ou_123", content="Thinking", metadata={"_progress": True, "turn_id": "turn-1"})

    await FeishuChannel.send(channel, msg)

    channel._streaming.handle.assert_awaited_once_with(msg)
    channel._outbound.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_feishu_channel_marks_turn_final_as_reply_post_and_cleans_up_on_success() -> None:
    channel = _build_channel()
    channel._outbound.send = AsyncMock(return_value=True)
    channel._streaming.cleanup_turn = AsyncMock(return_value=True)
    msg = OutboundMessage(
        channel="feishu",
        chat_id="ou_123",
        content="Final answer",
        metadata={"turn_id": "turn-2", "message_id": "om_source_2"},
    )

    await FeishuChannel.send(channel, msg)

    channel._streaming.handle.assert_not_awaited()
    delivered = channel._outbound.send.await_args.args[0]
    assert delivered is not msg
    assert delivered.metadata["feishu_delivery"] == "reply_post"
    assert delivered.metadata["turn_id"] == "turn-2"
    channel._streaming.cleanup_turn.assert_awaited_once_with("turn-2")
    channel._archive_service.kick_worker.assert_called_once_with()


@pytest.mark.asyncio
async def test_feishu_channel_keeps_placeholder_when_final_send_fails() -> None:
    channel = _build_channel()
    channel._outbound.send = AsyncMock(return_value=False)
    channel._streaming.cleanup_turn = AsyncMock(return_value=True)
    msg = OutboundMessage(
        channel="feishu",
        chat_id="ou_123",
        content="Final answer",
        metadata={"turn_id": "turn-3", "message_id": "om_source_3"},
    )

    await FeishuChannel.send(channel, msg)

    channel._streaming.cleanup_turn.assert_not_awaited()
    channel._archive_service.kick_worker.assert_not_called()


@pytest.mark.asyncio
async def test_feishu_channel_passes_non_turn_messages_through_unchanged() -> None:
    channel = _build_channel()
    channel._outbound.send = AsyncMock(return_value=True)
    msg = OutboundMessage(channel="feishu", chat_id="ou_123", content="Heartbeat")

    await FeishuChannel.send(channel, msg)

    channel._outbound.send.assert_awaited_once_with(msg)
    channel._streaming.cleanup_turn.assert_not_awaited()
    channel._archive_service.kick_worker.assert_not_called()


@pytest.mark.asyncio
async def test_feishu_channel_final_without_archive_service_still_sends() -> None:
    channel = _build_channel()
    channel._archive_service = None
    channel._outbound.send = AsyncMock(return_value=True)
    channel._streaming.cleanup_turn = AsyncMock(return_value=True)
    msg = OutboundMessage(
        channel="feishu",
        chat_id="ou_123",
        content="Final answer",
        metadata={"turn_id": "turn-4", "message_id": "om_source_4"},
    )

    await FeishuChannel.send(channel, msg)

    channel._outbound.send.assert_awaited_once()
    channel._streaming.cleanup_turn.assert_awaited_once_with("turn-4")
