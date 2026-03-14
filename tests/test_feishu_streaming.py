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
        self.created: list[tuple[str, str, str, dict, str | None]] = []
        self.patched: list[tuple[str, str, dict]] = []
        self.fail_create = False
        self.fail_patch = False
        self.created_ids = 0

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
        self.created_ids += 1
        return f"om_created_{self.created_ids}"

    def patch_message_sync(self, message_id: str, msg_type: str, content: str) -> bool:
        self.patched.append((message_id, msg_type, json.loads(content)))
        return not self.fail_patch


def _msg(content: str, **metadata) -> OutboundMessage:
    return OutboundMessage(channel="feishu", chat_id="ou_123", content=content, metadata=metadata)


@pytest.mark.asyncio
async def test_streamer_prepare_turn_only_registers_turn_without_creating_card() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        sleep=clock.sleep,
    )

    prepared = await streamer.prepare_turn(
        turn_id="turn-1",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_1",
    )

    assert prepared is True
    assert client.created == []
    assert client.patched == []
    assert await streamer.has_active_stream("turn-1") is False


@pytest.mark.asyncio
async def test_streamer_first_progress_patches_immediately() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        sleep=clock.sleep,
    )
    await streamer.prepare_turn(
        turn_id="turn-2",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_2",
    )

    handled = await streamer.handle(
        _msg(
            'web_search("AI 最新进展")',
            turn_id="turn-2",
            chat_type="p2p",
            message_id="om_source_2",
            _progress=True,
            _tool_hint=True,
        )
    )

    assert handled is True
    assert clock.delays == []
    assert client.created == [
        (
            "open_id",
            "ou_123",
            "interactive",
            {"config": {"wide_screen_mode": True}, "elements": [{"tag": "markdown", "content": "…"}]},
            "om_source_2",
        )
    ]
    assert client.patched == [
        (
            "om_created_1",
            "interactive",
            {
                "config": {"wide_screen_mode": True},
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "> 思考中…\n> 正在搜索网络：AI 最新进展",
                    }
                ],
            },
        )
    ]


@pytest.mark.asyncio
async def test_streamer_coalesces_follow_up_progress_with_throttle() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        sleep=clock.sleep,
    )
    await streamer.prepare_turn(
        turn_id="turn-3",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_3",
    )

    await streamer.handle(
        _msg(
            'read_file("/tmp/a.txt")',
            turn_id="turn-3",
            chat_type="p2p",
            message_id="om_source_3",
            _progress=True,
            _tool_hint=True,
        )
    )
    await streamer.handle(
        _msg(
            "正在整理结果",
            turn_id="turn-3",
            chat_type="p2p",
            message_id="om_source_3",
            _progress=True,
        )
    )
    await streamer.wait_for_idle()

    assert len(clock.delays) == 1
    assert clock.delays[0] == pytest.approx(0.5, abs=0.01)
    assert len(client.patched) == 2
    assert client.patched[-1][2]["elements"][0]["content"].endswith("> 正在整理结果")


@pytest.mark.asyncio
async def test_streamer_skips_duplicate_progress_entries() -> None:
    client = _FakeClient()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
    )
    await streamer.prepare_turn(
        turn_id="turn-4",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_4",
    )

    await streamer.handle(
        _msg(
            "正在整理结果",
            turn_id="turn-4",
            chat_type="p2p",
            message_id="om_source_4",
            _progress=True,
        )
    )
    await streamer.handle(
        _msg(
            "正在整理结果",
            turn_id="turn-4",
            chat_type="p2p",
            message_id="om_source_4",
            _progress=True,
        )
    )
    await streamer.wait_for_idle()

    assert len(client.patched) == 1


@pytest.mark.asyncio
async def test_streamer_complete_turn_with_meaningful_entries_keeps_completed_card() -> None:
    client = _FakeClient()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
    )
    await streamer.prepare_turn(
        turn_id="turn-5",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_5",
    )
    await streamer.handle(
        _msg(
            'exec("pytest")',
            turn_id="turn-5",
            chat_type="p2p",
            message_id="om_source_5",
            _progress=True,
            _tool_hint=True,
        )
    )

    completed = await streamer.complete_turn("turn-5")

    assert completed is True
    assert client.patched[-1][2]["elements"][0]["content"].endswith("> 思考完成")
    assert await streamer.has_active_stream("turn-5") is False


@pytest.mark.asyncio
async def test_streamer_complete_turn_without_meaningful_entries_uses_minimal_card() -> None:
    client = _FakeClient()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
    )
    await streamer.prepare_turn(
        turn_id="turn-6",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_6",
    )

    completed = await streamer.complete_turn("turn-6")

    assert completed is False
    assert client.created == []
    assert client.patched == []


@pytest.mark.asyncio
async def test_streamer_skips_group_messages_when_scope_is_dm() -> None:
    client = _FakeClient()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
    )

    handled = await streamer.handle(
        _msg("Thinking", turn_id="turn-7", chat_type="group", message_id="om_source_7", _progress=True)
    )

    assert handled is False
    assert client.created == []


@pytest.mark.asyncio
async def test_streamer_falls_back_to_on_demand_creation_when_not_prepared(monkeypatch) -> None:
    client = _FakeClient()
    warnings: list[str] = []
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
    )

    monkeypatch.setattr(
        "nanobot.feishu.streaming.logger.warning",
        lambda message, *args: warnings.append(message.format(*args)),
    )
    handled = await streamer.handle(
        _msg(
            'list_dir("/tmp")',
            turn_id="turn-8",
            chat_type="p2p",
            message_id="om_source_8",
            _progress=True,
            _tool_hint=True,
        )
    )

    assert handled is True
    assert warnings
    assert "not prepared" in warnings[0]
    assert len(client.created) == 1
    assert len(client.patched) == 1


@pytest.mark.asyncio
async def test_streamer_create_failure_disables_turn() -> None:
    client = _FakeClient()
    client.fail_create = True
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
    )

    prepared = await streamer.prepare_turn(
        turn_id="turn-9",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_9",
    )
    handled = await streamer.handle(
        _msg(
            "正在处理",
            turn_id="turn-9",
            chat_type="p2p",
            message_id="om_source_9",
            _progress=True,
        )
    )

    assert prepared is True
    assert handled is True
    assert len(client.created) == 1
    assert client.patched == []
    assert await streamer.has_active_stream("turn-9") is False


@pytest.mark.asyncio
async def test_streamer_can_prepare_new_turn_after_completion() -> None:
    client = _FakeClient()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
    )

    await streamer.prepare_turn(turn_id="turn-a", chat_id="ou_123", metadata={"chat_type": "p2p"}, reply_to="om_source_a")
    await streamer.complete_turn("turn-a")
    prepared = await streamer.prepare_turn(turn_id="turn-b", chat_id="ou_123", metadata={"chat_type": "p2p"}, reply_to="om_source_b")

    assert prepared is True
    assert await streamer.has_active_stream("turn-a") is False
    assert await streamer.has_active_stream("turn-b") is False


@pytest.mark.asyncio
async def test_streamer_keeps_turns_isolated_for_fast_consecutive_messages() -> None:
    client = _FakeClient()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
    )

    await streamer.prepare_turn(turn_id="turn-x", chat_id="ou_123", metadata={"chat_type": "p2p"}, reply_to="om_source_x")
    await streamer.prepare_turn(turn_id="turn-y", chat_id="ou_123", metadata={"chat_type": "p2p"}, reply_to="om_source_y")
    await streamer.handle(
        _msg(
            'web_fetch("https://example.com")',
            turn_id="turn-y",
            chat_type="p2p",
            message_id="om_source_y",
            _progress=True,
            _tool_hint=True,
        )
    )
    await streamer.complete_turn("turn-x")

    assert await streamer.has_active_stream("turn-x") is False
    assert await streamer.has_active_stream("turn-y") is True
    assert len(client.created) == 1
    assert client.created[0][4] == "om_source_y"
