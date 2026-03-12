from __future__ import annotations

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
        self.fail_create = False
        self.fail_patch_times = 0

    def create_message_sync(
        self,
        receive_id_type: str,
        receive_id: str,
        msg_type: str,
        content: str,
        reply_to: str | None = None,
    ) -> str | None:
        self.created.append((receive_id_type, receive_id, msg_type, json.loads(content), reply_to))
        if self.fail_create:
            return None
        return "om_card_1"

    def patch_message_sync(self, message_id: str, msg_type: str, content: str) -> bool:
        self.patched.append((message_id, msg_type, json.loads(content)))
        if self.fail_patch_times > 0:
            self.fail_patch_times -= 1
            return False
        return True

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

    prepared = await streamer.prepare_turn(
        turn_id="turn-1",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_1",
    )
    handled_progress = await streamer.handle(_msg("Thinking", turn_id="turn-1", stream_id="stream-1", chat_type="p2p", message_id="om_source_1", _progress=True))
    handled_final = await streamer.handle(_msg("Final answer", turn_id="turn-1", stream_id="stream-1", chat_type="p2p", message_id="om_source_1"))

    assert prepared is True
    assert handled_progress is True
    assert handled_final is True
    assert len(client.created) == 1
    assert client.created[0][2] == "interactive"
    assert client.created[0][4] == "om_source_1"
    assert "header" not in client.created[0][3]
    assert client.created[0][3]["elements"][0]["content"] == "…"
    assert len(client.patched) == 2
    assert client.patched[0][2]["header"]["title"]["content"] == "思考中…"
    assert client.patched[0][2]["elements"][0]["content"] == "…"
    assert "header" not in client.patched[1][2]
    assert "Final answer" in json.dumps(client.patched[1][2], ensure_ascii=False)
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

    await streamer.prepare_turn(
        turn_id="turn-2",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_2",
    )
    await streamer.handle(_msg("Thinking", turn_id="turn-2", stream_id="stream-2", chat_type="p2p", message_id="om_source_2", _progress=True))
    await streamer.handle(_msg('read_file("a")', turn_id="turn-2", stream_id="stream-2", chat_type="p2p", message_id="om_source_2", _progress=True, _tool_hint=True))
    await streamer.handle(_msg('web_search("b")', turn_id="turn-2", stream_id="stream-2", chat_type="p2p", message_id="om_source_2", _progress=True, _tool_hint=True))

    assert len(client.patched) == 1
    assert client.patched[0][2]["header"]["title"]["content"] == "思考中…"
    await streamer.wait_for_idle()
    assert len(client.patched) == 2
    assert client.patched[1][2]["header"]["title"]["content"] == "正在搜索网络"
    assert client.patched[1][2]["elements"][0]["content"] == "…"


@pytest.mark.asyncio
async def test_streamer_retries_patch_once_then_falls_back_to_plain_message_when_patch_still_fails() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        now=clock.now,
        sleep=clock.sleep,
    )

    await streamer.prepare_turn(
        turn_id="turn-3",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_3",
    )
    await streamer.handle(_msg("Thinking", turn_id="turn-3", stream_id="stream-3", chat_type="p2p", message_id="om_source_3", _progress=True))
    client.fail_patch_times = 2
    handled_final = await streamer.handle(_msg("Final answer", turn_id="turn-3", stream_id="stream-3", chat_type="p2p", message_id="om_source_3"))

    assert handled_final is True
    assert len(client.patched) == 3
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


@pytest.mark.asyncio
async def test_streamer_maps_exec_tool_hint_to_executing_title() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        now=clock.now,
        sleep=clock.sleep,
    )

    await streamer.prepare_turn(
        turn_id="turn-5",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_5",
    )
    await streamer.handle(_msg("Thinking", turn_id="turn-5", stream_id="stream-5", chat_type="p2p", message_id="om_source_5", _progress=True))
    await streamer.handle(_msg('exec("ls")', turn_id="turn-5", stream_id="stream-5", chat_type="p2p", message_id="om_source_5", _progress=True, _tool_hint=True))
    await streamer.wait_for_idle()

    assert client.patched[-1][2]["header"]["title"]["content"] == "正在执行操作"


@pytest.mark.asyncio
async def test_streamer_can_finalize_prepared_placeholder_without_progress() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        now=clock.now,
        sleep=clock.sleep,
    )

    await streamer.prepare_turn(
        turn_id="turn-5b",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_5b",
    )
    handled = await streamer.handle(_msg("Final answer", turn_id="turn-5b", stream_id="stream-5b", chat_type="p2p", message_id="om_source_5b"))

    assert handled is True
    assert len(client.created) == 1
    assert len(client.patched) == 1
    assert "header" not in client.patched[0][2]
    assert client.patched[0][2]["elements"][0]["content"] == "Final answer"


@pytest.mark.asyncio
async def test_streamer_final_cancels_pending_progress_flush() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        now=clock.now,
        sleep=clock.sleep,
    )

    await streamer.prepare_turn(
        turn_id="turn-6",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_6",
    )
    await streamer.handle(_msg("Thinking", turn_id="turn-6", stream_id="stream-6", chat_type="p2p", message_id="om_source_6", _progress=True))
    await streamer.handle(_msg('read_file("a")', turn_id="turn-6", stream_id="stream-6", chat_type="p2p", message_id="om_source_6", _progress=True, _tool_hint=True))
    await streamer.handle(_msg("Final answer", turn_id="turn-6", stream_id="stream-6", chat_type="p2p", message_id="om_source_6"))
    await streamer.wait_for_idle()

    assert len(client.patched) == 2
    assert "header" not in client.patched[-1][2]
    assert client.sent == []


@pytest.mark.asyncio
async def test_streamer_create_failure_disables_progress_and_allows_final_fallback() -> None:
    client = _FakeClient()
    client.fail_create = True
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        now=clock.now,
        sleep=clock.sleep,
    )

    prepared = await streamer.prepare_turn(
        turn_id="turn-7",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_7",
    )
    handled_progress = await streamer.handle(_msg("Thinking", turn_id="turn-7", stream_id="stream-7", chat_type="p2p", message_id="om_source_7", _progress=True))
    handled_final = await streamer.handle(_msg("Final answer", turn_id="turn-7", stream_id="stream-7", chat_type="p2p", message_id="om_source_7"))

    assert prepared is False
    assert handled_progress is True
    assert handled_final is False
    assert client.created == [("open_id", "ou_123", "interactive", {"config": {"wide_screen_mode": True}, "elements": [{"tag": "markdown", "content": "…"}]}, "om_source_7")]
    assert client.patched == []
    assert client.sent == []


@pytest.mark.asyncio
async def test_streamer_warns_and_creates_on_demand_when_not_prepared(monkeypatch) -> None:
    client = _FakeClient()
    clock = _Clock()
    warnings: list[str] = []
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        now=clock.now,
        sleep=clock.sleep,
    )

    monkeypatch.setattr("nanobot.feishu.streaming.logger.warning", lambda message, *args: warnings.append(message.format(*args)))

    handled = await streamer.handle(_msg("Thinking", turn_id="turn-8", stream_id="stream-8", chat_type="p2p", message_id="om_source_8", _progress=True))

    assert handled is True
    assert warnings
    assert "placeholder not pre-created" in warnings[0]
    assert client.created[0][3]["header"]["title"]["content"] == "思考中…"
