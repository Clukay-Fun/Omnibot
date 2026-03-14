from __future__ import annotations

import asyncio
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


class _ManualSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []
        self._waiters: list[asyncio.Future[None]] = []

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        await fut

    async def release_next(self) -> None:
        if not self._waiters:
            return
        waiter = self._waiters.pop(0)
        if not waiter.done():
            waiter.set_result(None)
        await asyncio.sleep(0)


class _FakeClient:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str, dict, str | None]] = []
        self.patched: list[tuple[str, str, dict]] = []
        self.deleted: list[str] = []
        self.fail_create = False
        self.fail_patch = False
        self.fail_delete = False
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

    def delete_message_sync(self, message_id: str) -> bool:
        self.deleted.append(message_id)
        return not self.fail_delete


def _msg(content: str, **metadata) -> OutboundMessage:
    return OutboundMessage(channel="feishu", chat_id="ou_123", content=content, metadata=metadata)


async def _drain_loop(turns: int = 3) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


def _element_texts(payload: dict) -> list[str]:
    texts: list[str] = []
    for element in payload.get("elements", []):
        if element.get("tag") == "note":
            for child in element.get("elements", []):
                text = child.get("content")
                if isinstance(text, str):
                    texts.append(text)
        elif element.get("tag") == "markdown":
            text = element.get("content")
            if isinstance(text, str):
                texts.append(text)
    return texts


@pytest.mark.asyncio
async def test_streamer_prepare_turn_only_registers_turn_without_creating_card() -> None:
    client = _FakeClient()
    streamer = FeishuCardStreamer(client_getter=lambda: cast(object, client), scope="dm", throttle_seconds=0.5)

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
    await streamer.cleanup_turn("turn-1")


@pytest.mark.asyncio
async def test_streamer_first_tool_progress_patches_immediately() -> None:
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
            _is_tool_progress=True,
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
            {
                "config": {"wide_screen_mode": True},
                "elements": [{"tag": "note", "elements": [{"tag": "plain_text", "content": "▏ …"}]}],
            },
            "om_source_2",
        )
    ]
    assert _element_texts(client.patched[0][2]) == ["▏ 思考中…", "▏ 正在搜索网络：AI 最新进展"]
    await streamer.cleanup_turn("turn-2")


@pytest.mark.asyncio
async def test_streamer_non_tool_progress_does_not_create_card() -> None:
    client = _FakeClient()
    notice_sleep = _ManualSleep()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        notice_sleep=notice_sleep.sleep,
    )

    await streamer.prepare_turn(
        turn_id="turn-non-tool",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_non_tool",
    )
    handled = await streamer.handle(
        _msg(
            "正在整理结果",
            turn_id="turn-non-tool",
            chat_type="p2p",
            message_id="om_source_non_tool",
            _progress=True,
            _is_tool_progress=False,
        )
    )

    assert handled is True
    assert client.created == []
    assert client.patched == []
    await streamer.cleanup_turn("turn-non-tool")


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
            _is_tool_progress=True,
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
    assert _element_texts(client.patched[-1][2])[-1] == "▏ 正在整理结果"
    await streamer.cleanup_turn("turn-3")


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
            'read_file("/tmp/a.txt")',
            turn_id="turn-4",
            chat_type="p2p",
            message_id="om_source_4",
            _progress=True,
            _is_tool_progress=True,
            _tool_hint=True,
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

    assert len(client.patched) == 2
    assert _element_texts(client.patched[-1][2])[-1] == "▏ 正在整理结果"
    await streamer.cleanup_turn("turn-4")


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
            _is_tool_progress=True,
            _tool_hint=True,
        )
    )

    completed = await streamer.complete_turn("turn-5")

    assert completed is True
    assert _element_texts(client.patched[-1][2])[-1] == "▏ 思考完成"
    assert await streamer.has_active_stream("turn-5") is False


@pytest.mark.asyncio
async def test_streamer_complete_turn_without_any_feedback_is_noop() -> None:
    client = _FakeClient()
    notice_sleep = _ManualSleep()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        notice_sleep=notice_sleep.sleep,
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
async def test_streamer_sends_delayed_thinking_card_for_slow_non_tool_turn() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        notice_sleep=clock.sleep,
    )

    await streamer.prepare_turn(
        turn_id="turn-notice",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_notice",
    )
    await _drain_loop()

    assert clock.delays == [5.0]
    assert client.created == [
        (
            "open_id",
            "ou_123",
            "interactive",
            {
                "config": {"wide_screen_mode": True},
                "elements": [{"tag": "note", "elements": [{"tag": "plain_text", "content": "▏ 思考中…"}]}],
            },
            "om_source_notice",
        )
    ]
    assert client.patched == []
    assert await streamer.has_active_stream("turn-notice") is True

    completed = await streamer.complete_turn("turn-notice")

    assert completed is True
    assert client.deleted == []
    assert _element_texts(client.patched[-1][2])[-1] == "▏ 思考完成"


@pytest.mark.asyncio
async def test_streamer_tool_progress_before_delayed_card_creates_card_immediately() -> None:
    client = _FakeClient()
    throttle_clock = _Clock()
    notice_sleep = _ManualSleep()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        sleep=throttle_clock.sleep,
        notice_sleep=notice_sleep.sleep,
    )

    await streamer.prepare_turn(
        turn_id="turn-tool-first",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_tool_first",
    )
    await streamer.handle(
        _msg(
            'web_search("测试查询")',
            turn_id="turn-tool-first",
            chat_type="p2p",
            message_id="om_source_tool_first",
            _progress=True,
            _is_tool_progress=True,
            _tool_hint=True,
        )
    )

    assert len(client.created) == 1
    assert client.created[0][2] == "interactive"
    assert _element_texts(client.patched[-1][2])[-1] == "▏ 正在搜索网络：测试查询"
    await streamer.cleanup_turn("turn-tool-first")


@pytest.mark.asyncio
async def test_streamer_appends_tool_progress_into_existing_delayed_thinking_card() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        notice_sleep=clock.sleep,
    )

    await streamer.prepare_turn(
        turn_id="turn-notice-then-tool",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_notice_then_tool",
    )
    await _drain_loop()

    assert client.created == [
        (
            "open_id",
            "ou_123",
            "interactive",
            {
                "config": {"wide_screen_mode": True},
                "elements": [{"tag": "note", "elements": [{"tag": "plain_text", "content": "▏ 思考中…"}]}],
            },
            "om_source_notice_then_tool",
        )
    ]

    handled = await streamer.handle(
        _msg(
            'exec("pytest")',
            turn_id="turn-notice-then-tool",
            chat_type="p2p",
            message_id="om_source_notice_then_tool",
            _progress=True,
            _is_tool_progress=True,
            _tool_hint=True,
        )
    )

    assert handled is True
    assert len(client.created) == 1
    assert _element_texts(client.patched[-1][2])[-1] == "▏ 正在执行操作：pytest"


@pytest.mark.asyncio
async def test_streamer_slow_non_tool_turn_keeps_completed_card_without_delete() -> None:
    client = _FakeClient()
    clock = _Clock()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
        notice_sleep=clock.sleep,
    )

    await streamer.prepare_turn(
        turn_id="turn-slow-complete",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_slow_complete",
    )
    await _drain_loop()

    completed = await streamer.complete_turn("turn-slow-complete")

    assert completed is True
    assert client.deleted == []
    assert _element_texts(client.patched[-1][2])[-1] == "▏ 思考完成"


@pytest.mark.asyncio
async def test_streamer_maps_feishu_workspace_skill_file_and_exec_to_human_text() -> None:
    client = _FakeClient()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
    )
    await streamer.prepare_turn(
        turn_id="turn-skill",
        chat_id="ou_123",
        metadata={"chat_type": "p2p"},
        reply_to="om_source_skill",
    )

    await streamer.handle(
        _msg(
            'read_file("/Users/clukay/Program/ominibot/nanobot/skills/feishu-workspace/references/bitable.md")',
            turn_id="turn-skill",
            chat_type="p2p",
            message_id="om_source_skill",
            _progress=True,
            _is_tool_progress=True,
            _tool_hint=True,
        )
    )
    await streamer.handle(
        _msg(
            'exec("bash /Users/clukay/Program/ominibot/nanobot/skills/feishu-workspace/scripts/bitable.sh table list --app-token demo")',
            turn_id="turn-skill",
            chat_type="p2p",
            message_id="om_source_skill",
            _progress=True,
            _is_tool_progress=True,
            _tool_hint=True,
        )
    )
    await streamer.wait_for_idle()

    texts = _element_texts(client.patched[-1][2])
    assert "▏ 正在读取多维表格能力说明" in texts
    assert "▏ 正在列出多维表格" in texts
    await streamer.cleanup_turn("turn-skill")


@pytest.mark.asyncio
async def test_streamer_skips_group_messages_when_scope_is_dm() -> None:
    client = _FakeClient()
    streamer = FeishuCardStreamer(
        client_getter=lambda: cast(object, client),
        scope="dm",
        throttle_seconds=0.5,
    )

    handled = await streamer.handle(
        _msg(
            "Thinking",
            turn_id="turn-7",
            chat_type="group",
            message_id="om_source_7",
            _progress=True,
            _is_tool_progress=True,
        )
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
            _is_tool_progress=True,
            _tool_hint=True,
        )
    )

    assert handled is True
    assert warnings
    assert "not prepared" in warnings[0]
    assert len(client.created) == 1
    assert len(client.patched) == 1
    await streamer.cleanup_turn("turn-8")


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
            'read_file("/tmp/b.txt")',
            turn_id="turn-9",
            chat_type="p2p",
            message_id="om_source_9",
            _progress=True,
            _is_tool_progress=True,
            _tool_hint=True,
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
    await streamer.cleanup_turn("turn-b")


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
            _is_tool_progress=True,
            _tool_hint=True,
        )
    )
    await streamer.complete_turn("turn-x")

    assert await streamer.has_active_stream("turn-x") is False
    assert await streamer.has_active_stream("turn-y") is True
    assert len(client.created) == 1
    assert client.created[0][4] == "om_source_y"
    await streamer.cleanup_turn("turn-y")
