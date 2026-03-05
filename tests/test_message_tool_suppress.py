"""Test message tool suppress logic for final replies."""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.message import MessageTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse, ToolCallRequest


def _make_loop(tmp_path: Path) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model", memory_window=10)


class TestMessageToolSuppressLogic:
    """Final reply suppressed only when message tool sends to the same target."""

    @pytest.mark.asyncio
    async def test_suppress_when_sent_to_same_target(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(
            id="call1", name="message",
            arguments={"content": "Hello", "channel": "feishu", "chat_id": "chat123"},
        )
        calls = iter([
            LLMResponse(content="", tool_calls=[tool_call]),
            LLMResponse(content="Done", tool_calls=[]),
        ])
        loop.provider.chat = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])

        sent: list[OutboundMessage] = []
        mt = loop.tools.get("message")
        if isinstance(mt, MessageTool):
            mt.set_send_callback(AsyncMock(side_effect=lambda m: sent.append(m)))

        msg = InboundMessage(channel="feishu", sender_id="user1", chat_id="chat123", content="Send")
        result = await loop._process_message(msg)

        assert len(sent) == 1
        assert result is None  # suppressed

    @pytest.mark.asyncio
    async def test_not_suppress_when_sent_to_different_target(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(
            id="call1", name="message",
            arguments={"content": "Email content", "channel": "email", "chat_id": "user@example.com"},
        )
        calls = iter([
            LLMResponse(content="", tool_calls=[tool_call]),
            LLMResponse(content="I've sent the email.", tool_calls=[]),
        ])
        loop.provider.chat = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])

        sent: list[OutboundMessage] = []
        mt = loop.tools.get("message")
        if isinstance(mt, MessageTool):
            mt.set_send_callback(AsyncMock(side_effect=lambda m: sent.append(m)))

        msg = InboundMessage(channel="feishu", sender_id="user1", chat_id="chat123", content="Send email")
        result = await loop._process_message(msg)

        assert len(sent) == 1
        assert sent[0].channel == "email"
        assert result is not None  # not suppressed
        assert result.channel == "feishu"

    @pytest.mark.asyncio
    async def test_not_suppress_when_no_message_tool_used(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop.provider.chat = AsyncMock(return_value=LLMResponse(content="Hello!", tool_calls=[]))
        loop.tools.get_definitions = MagicMock(return_value=[])

        msg = InboundMessage(channel="feishu", sender_id="user1", chat_id="chat123", content="Hi")
        result = await loop._process_message(msg)

        assert result is not None
        assert "Hello" in result.content


class TestMessageToolTurnTracking:

    def test_sent_in_turn_tracks_same_target(self) -> None:
        tool = MessageTool()
        tool.set_context("feishu", "chat1")
        assert not tool._sent_in_turn
        tool._sent_in_turn = True
        assert tool._sent_in_turn

    def test_start_turn_resets(self) -> None:
        tool = MessageTool()
        tool._sent_in_turn = True
        tool.start_turn()
        assert not tool._sent_in_turn


class TestAgentSlashCommands:
    @pytest.mark.asyncio
    async def test_commands_show_short_descriptions(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        msg = InboundMessage(channel="feishu", sender_id="u1", chat_id="oc_1", content="/commands")

        response = await loop._process_message(msg)

        assert response is not None
        assert "可用指令" in response.content
        assert "/help 或 /commands" in response.content
        assert "/session new" in response.content
        assert "/session del" in response.content

    @pytest.mark.asyncio
    async def test_session_new_marks_reply_in_thread_for_feishu(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        msg = InboundMessage(
            channel="feishu",
            sender_id="u1",
            chat_id="oc_1",
            content="/session new",
            metadata={"message_id": "m1"},
        )

        response = await loop._process_message(msg)

        assert response is not None
        assert response.metadata.get("_start_topic_session") is True
        assert response.metadata.get("_reply_in_thread") is True
        assert re.fullmatch(r"会话-\d{8}-\d{4}", response.content)

    @pytest.mark.asyncio
    async def test_session_new_with_custom_title(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        msg = InboundMessage(
            channel="feishu",
            sender_id="u1",
            chat_id="oc_1",
            content="/session new xx任务",
            metadata={"message_id": "m1"},
        )

        response = await loop._process_message(msg)

        assert response is not None
        assert response.content == "xx任务"
        assert response.metadata.get("_start_topic_session") is True
        assert response.metadata.get("_reply_in_thread") is True

    @pytest.mark.asyncio
    async def test_session_list_shows_main_and_thread_sessions(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        main = loop.sessions.get_or_create("feishu:oc_1")
        main.add_message("user", "hello")
        loop.sessions.save(main)

        thread = loop.sessions.get_or_create("feishu:oc_1:thread_a")
        thread.add_message("user", "topic")
        loop.sessions.save(thread)

        msg = InboundMessage(
            channel="feishu",
            sender_id="u1",
            chat_id="oc_1",
            content="/session list",
            session_key_override="feishu:oc_1:thread_a",
        )

        response = await loop._process_message(msg)

        assert response is not None
        assert "当前聊天会话列表" in response.content
        assert "1. main（主会话）" in response.content
        assert "thread_a（当前）" in response.content

    @pytest.mark.asyncio
    async def test_session_list_includes_pending_topic_created_by_session_new(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)

        new_resp = await loop._process_message(
            InboundMessage(
                channel="feishu",
                sender_id="u1",
                chat_id="oc_1",
                content="/session new 立案推进",
                metadata={"message_id": "m-new-1"},
            )
        )
        assert new_resp is not None

        list_resp = await loop._process_message(
            InboundMessage(
                channel="feishu",
                sender_id="u1",
                chat_id="oc_1",
                content="/session list",
            )
        )

        assert list_resp is not None
        assert "待激活话题：" in list_resp.content
        assert "立案推进（待激活）" in list_resp.content

    @pytest.mark.asyncio
    async def test_session_list_consumes_one_pending_topic_after_entering_thread(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)

        await loop._process_message(
            InboundMessage(
                channel="feishu",
                sender_id="u1",
                chat_id="oc_1",
                content="/session new 合同审查",
                metadata={"message_id": "m-new-2"},
            )
        )

        in_thread = await loop._process_message(
            InboundMessage(
                channel="feishu",
                sender_id="u1",
                chat_id="oc_1",
                content="/session list",
                session_key_override="feishu:oc_1:thread_created",
            )
        )
        assert in_thread is not None
        assert "待激活话题：" not in in_thread.content

    @pytest.mark.asyncio
    async def test_session_del_main_removes_main_session(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        main = loop.sessions.get_or_create("feishu:oc_1")
        main.add_message("user", "hello")
        loop.sessions.save(main)

        msg = InboundMessage(channel="feishu", sender_id="u1", chat_id="oc_1", content="/session del main")
        response = await loop._process_message(msg)

        assert response is not None
        assert "已删除会话" in response.content
        keys = {str(item.get("key") or "") for item in loop.sessions.list_sessions()}
        assert "feishu:oc_1" not in keys
