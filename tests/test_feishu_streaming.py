from __future__ import annotations

import asyncio
import json
from typing import cast

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.feishu.streaming import FeishuCardStreamer


class _Clock:
    def __init__(self) -> None:
        self.now_value = 0.0

    def now(self) -> float:
        return self.now_value

    async def sleep(self, delay: float) -> None:
        self.now_value += delay


class _FakeClient:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str, dict, str | None]] = []
        self.patched: list[tuple[str, str, dict]] = []
        self.sent: list[tuple[str, str, str, dict, str | None]] = []
        self.fail_patch = False

    def create_message_sync(
        self,
        receive_id_type: str,
        receive_id: str,
        msg_type: str,
        content: str,
        reply_to: str | None = None,
    ) -> str | None:
        self.created.append((receive_id_type, receive_id, msg_type, json.loads(content), reply_to))
        return "om_card_1"

    def patch_message_sync(self, message_id: str, msg_type: str, content: str) -> bool:
        self.patched.append((message_id, msg_type, json.loads(content)))
        return not self.fail_patch

    def send_message_sync(
        self,
        receive_id_type: str,
        receive_id: str,
        msg_type: str,
        content: str,
        reply_to: str | None = None,
    ) -> bool:
        self.sent.append((receive_id_type, receive_id, msg_type, json.loads(content), reply_to))
        return True


def _msg(content: str, **metadata) -> OutboundMessage:
    return OutboundMessage(channel="feishu", chat_id="ou_123", content=content, metadata=metadata)


@pytest.mark.asyncio
async def test_streamer_creates_card_for_progress_and_patches_final() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        now=clock.now,
        sleep=clock.sleep,
    )

    handled_progress = await streamer.handle(_msg("Thinking", turn_id="turn-1", stream_id="stream-1", chat_type="p2p", message_id="om_source_1", _progress=True))
    handled_final = await streamer.handle(_msg("Final answer", turn_id="turn-1", stream_id="stream-1", chat_type="p2p", message_id="om_source_1"))

    assert handled_progress is True
    assert handled_final is True
    assert len(client.created) == 1
    assert client.created[0][2] == "interactive"
    assert client.created[0][4] == "om_source_1"
    assert len(client.patched) == 1
    assert "Final answer" in json.dumps(client.patched[0][2], ensure_ascii=False)
    assert await streamer.has_active_stream("turn-1") is False


@pytest.mark.asyncio
async def test_streamer_coalesces_quick_progress_updates() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        now=clock.now,
        sleep=clock.sleep,
    )

    await streamer.handle(_msg("Thinking", turn_id="turn-2", stream_id="stream-2", chat_type="p2p", message_id="om_source_2", _progress=True))
    await streamer.handle(_msg('read_file("a")', turn_id="turn-2", stream_id="stream-2", chat_type="p2p", message_id="om_source_2", _progress=True, _tool_hint=True))
    await streamer.handle(_msg('web_search("b")', turn_id="turn-2", stream_id="stream-2", chat_type="p2p", message_id="om_source_2", _progress=True, _tool_hint=True))

    assert len(client.patched) == 0
    await streamer.wait_for_idle()
    assert len(client.patched) == 1
    assert "web_search" in json.dumps(client.patched[0][2], ensure_ascii=False)


@pytest.mark.asyncio
async def test_streamer_falls_back_to_plain_message_when_patch_fails() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        now=clock.now,
        sleep=clock.sleep,
    )

    await streamer.handle(_msg("Thinking", turn_id="turn-3", stream_id="stream-3", chat_type="p2p", message_id="om_source_3", _progress=True))
    client.fail_patch = True
    handled_final = await streamer.handle(_msg("Final answer", turn_id="turn-3", stream_id="stream-3", chat_type="p2p", message_id="om_source_3"))

    assert handled_final is True
    assert len(client.sent) == 1
    assert client.sent[0][2] == "text"
    assert client.sent[0][3] == {"text": "Final answer"}
    assert client.sent[0][4] == "om_source_3"


@pytest.mark.asyncio
async def test_streamer_skips_group_messages_when_scope_is_dm() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        now=clock.now,
        sleep=clock.sleep,
    )

    handled = await streamer.handle(_msg("Thinking", turn_id="turn-4", stream_id="stream-4", chat_type="group", message_id="om_source_4", _progress=True))

    assert handled is False
    assert client.created == []
