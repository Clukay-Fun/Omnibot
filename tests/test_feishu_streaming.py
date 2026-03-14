from __future__ import annotations

import json
from typing import cast

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.feishu.streaming import FeishuCardStreamer


class _Clock:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, dict, str | None]] = []
        self.fail_send = False

    def send_message_sync(
        self,
        receive_id_type: str,
        receive_id: str,
        msg_type: str,
        content: str,
        reply_to: str | None = None,
    ) -> bool:
        self.sent.append((receive_id_type, receive_id, msg_type, json.loads(content), reply_to))
        return not self.fail_send


def _msg(content: str, **metadata) -> OutboundMessage:
    return OutboundMessage(channel="feishu", chat_id="ou_123", content=content, metadata=metadata)


@pytest.mark.asyncio
async def test_streamer_sends_delayed_text_notice_for_slow_turn() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        notice_delay_seconds=2.0,
        sleep=clock.sleep,
    )

    prepared = await streamer.prepare_turn(
        turn_id="turn-1",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_1",
    )

    assert prepared is True
    assert client.sent == []

    await streamer.wait_for_idle()

    assert clock.delays == [2.0]
    assert client.sent == [("open_id", "ou_123", "text", {"text": "已收到，正在处理… 🙂"}, "om_source_1")]


@pytest.mark.asyncio
async def test_streamer_cleanup_before_delay_prevents_notice() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        notice_delay_seconds=2.0,
        sleep=clock.sleep,
    )

    await streamer.prepare_turn(
        turn_id="turn-2",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_2",
    )
    cleaned = await streamer.cleanup_turn("turn-2")
    await streamer.wait_for_idle()

    assert cleaned is True
    assert client.sent == []


@pytest.mark.asyncio
async def test_streamer_swallows_progress_without_extra_updates() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        notice_delay_seconds=2.0,
        sleep=clock.sleep,
    )

    await streamer.prepare_turn(
        turn_id="turn-3",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_3",
    )
    handled_1 = await streamer.handle(_msg("Thinking", turn_id="turn-3", chat_type="p2p", message_id="om_source_3", _progress=True))
    handled_2 = await streamer.handle(_msg('web_search("b")', turn_id="turn-3", chat_type="p2p", message_id="om_source_3", _progress=True, _tool_hint=True))
    await streamer.wait_for_idle()

    assert handled_1 is True
    assert handled_2 is True
    assert len(client.sent) == 1
    assert client.sent[0][3] == {"text": "已收到，正在处理… 🙂"}


@pytest.mark.asyncio
async def test_streamer_skips_group_messages_when_scope_is_dm() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        notice_delay_seconds=2.0,
        sleep=clock.sleep,
    )

    handled = await streamer.handle(_msg("Thinking", turn_id="turn-4", chat_type="group", message_id="om_source_4", _progress=True))

    assert handled is False
    assert client.sent == []


@pytest.mark.asyncio
async def test_streamer_falls_back_to_immediate_notice_when_not_prepared(monkeypatch) -> None:
    client = _FakeClient()
    clock = _Clock()
    warnings: list[str] = []
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        notice_delay_seconds=2.0,
        sleep=clock.sleep,
    )

    monkeypatch.setattr("nanobot.feishu.streaming.logger.warning", lambda message, *args: warnings.append(message.format(*args)))
    handled = await streamer.handle(_msg("Thinking", turn_id="turn-5", chat_type="p2p", message_id="om_source_5", _progress=True))

    assert handled is True
    assert warnings
    assert "not prepared" in warnings[0]
    assert client.sent == [("open_id", "ou_123", "text", {"text": "已收到，正在处理… 🙂"}, "om_source_5")]


@pytest.mark.asyncio
async def test_streamer_send_failure_disables_turn() -> None:
    client = _FakeClient()
    client.fail_send = True
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        notice_delay_seconds=2.0,
        sleep=clock.sleep,
    )

    await streamer.prepare_turn(
        turn_id="turn-6",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_6",
    )
    await streamer.wait_for_idle()
    handled = await streamer.handle(_msg("Thinking", turn_id="turn-6", chat_type="p2p", message_id="om_source_6", _progress=True))

    assert handled is True
    assert client.sent == [("open_id", "ou_123", "text", {"text": "已收到，正在处理… 🙂"}, "om_source_6")]
    assert await streamer.has_active_stream("turn-6") is False


@pytest.mark.asyncio
async def test_streamer_can_prepare_new_turn_after_cleanup() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        notice_delay_seconds=2.0,
        sleep=clock.sleep,
    )

    await streamer.prepare_turn(turn_id="turn-a", chat_id="ou_123", metadata={"chat_type": "p2p"}, reply_to="om_source_a")
    await streamer.cleanup_turn("turn-a")
    prepared = await streamer.prepare_turn(turn_id="turn-b", chat_id="ou_123", metadata={"chat_type": "p2p"}, reply_to="om_source_b")

    assert prepared is True
    assert await streamer.has_active_stream("turn-a") is False
    assert await streamer.has_active_stream("turn-b") is True


@pytest.mark.asyncio
async def test_streamer_keeps_turns_isolated_for_fast_consecutive_messages() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        notice_delay_seconds=2.0,
        sleep=clock.sleep,
    )

    await streamer.prepare_turn(turn_id="turn-x", chat_id="ou_123", metadata={"chat_type": "p2p"}, reply_to="om_source_x")
    await streamer.prepare_turn(turn_id="turn-y", chat_id="ou_123", metadata={"chat_type": "p2p"}, reply_to="om_source_y")
    await streamer.cleanup_turn("turn-x")
    await streamer.wait_for_idle()

    assert client.sent == [("open_id", "ou_123", "text", {"text": "已收到，正在处理… 🙂"}, "om_source_y")]
    assert await streamer.has_active_stream("turn-x") is False
    assert await streamer.has_active_stream("turn-y") is True
