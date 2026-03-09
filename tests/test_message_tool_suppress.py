"""Test message tool suppress logic for final replies."""

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.message import MessageTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ChannelsConfig, FeishuConfig, SkillSpecConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest


def _make_loop(tmp_path: Path) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model", memory_window=10)


class _BootstrapProvider:
    def __init__(self) -> None:
        self.last_messages: list[dict] = []

    async def chat(self, **kwargs):
        self.last_messages = list(kwargs.get("messages") or [])
        return LLMResponse(content="llm-fallback", tool_calls=[])

    def get_default_model(self) -> str:
        return "test-model"


class _WorkflowProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.last_tools = None

    async def chat(self, **kwargs):
        self.calls += 1
        self.last_tools = kwargs.get("tools")
        return LLMResponse(content="mode-aware-reply", tool_calls=[])

    def get_default_model(self) -> str:
        return "test-model"


class _DummyTool(Tool):
    @property
    def name(self) -> str:
        return "dummy_tool"

    @property
    def description(self) -> str:
        return "dummy"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs
        return "ok"


class TestMessageToolSuppressLogic:
    """Final reply suppressed only when message tool sends to the same target."""

    @pytest.mark.asyncio
    async def test_suppress_when_sent_to_same_target(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(
            id="call1", name="message",
            arguments={"content": "Hello", "channel": "cli", "chat_id": "chat123"},
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

        msg = InboundMessage(channel="cli", sender_id="user1", chat_id="chat123", content="Send")
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

        msg = InboundMessage(channel="cli", sender_id="user1", chat_id="chat123", content="Send email")
        result = await loop._process_message(msg)

        assert len(sent) == 1
        assert sent[0].channel == "email"
        assert result is not None  # not suppressed
        assert result.channel == "cli"

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
        assert "全局命令" in response.content
        assert "/setup" in response.content
        assert "上下文命令" in response.content
        assert "/plan" in response.content
        assert "/build" in response.content

    @pytest.mark.asyncio
    async def test_plan_command_switches_session_to_plan_mode(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)

        response = await loop._process_message(
            InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="/plan")
        )

        assert response is not None
        assert "plan 模式" in response.content
        session = loop.sessions.get_or_create("cli:chat")
        assert session.metadata["workflow_mode"] == "plan"

    @pytest.mark.asyncio
    async def test_status_shows_current_workflow_mode(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        session = loop.sessions.get_or_create("cli:chat")
        session.metadata["workflow_mode"] = "plan"
        loop.sessions.save(session)

        response = await loop._process_message(
            InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="/status")
        )

        assert response is not None
        assert "工作模式：计划（只读）" in response.content

    @pytest.mark.asyncio
    async def test_plan_mode_blocks_skillspec_execution(self, tmp_path: Path) -> None:
        skillspec_root = tmp_path / "skillspec"
        skillspec_root.mkdir(parents=True, exist_ok=True)
        (skillspec_root / "query_test.yaml").write_text(
            """
meta: {id: query_test, version: "0.1", description: 查询测试}
params: {type: object, properties: {query: {type: string}}}
action:
  kind: query
  table: {app_token: app_x, table_id: tbl_x}
response: {}
error: {}
""".strip()
            + "\n",
            encoding="utf-8",
        )
        provider = _WorkflowProvider()
        loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, skillspec_config=SkillSpecConfig(enabled=True))
        session = loop.sessions.get_or_create("cli:chat")
        session.metadata["workflow_mode"] = "plan"
        loop.sessions.save(session)

        response = await loop._process_message(
            InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="/skill query_test 关键词")
        )

        assert response is not None
        assert "plan 模式" in response.content
        assert provider.calls == 0

    @pytest.mark.asyncio
    async def test_plan_mode_keeps_chat_but_exposes_no_tools(self, tmp_path: Path) -> None:
        provider = _WorkflowProvider()
        loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, skillspec_config=SkillSpecConfig(enabled=False))
        loop.tools.register(_DummyTool())
        session = loop.sessions.get_or_create("cli:chat")
        session.metadata["workflow_mode"] = "plan"
        loop.sessions.save(session)

        response = await loop._process_message(
            InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="帮我规划一下合同录入流程")
        )

        assert response is not None
        assert response.content == "mode-aware-reply"
        assert provider.calls == 1
        assert provider.last_tools == []

    @pytest.mark.asyncio
    async def test_build_command_restores_tool_access(self, tmp_path: Path) -> None:
        provider = _WorkflowProvider()
        loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, skillspec_config=SkillSpecConfig(enabled=False))
        loop.tools.register(_DummyTool())
        session = loop.sessions.get_or_create("cli:chat")
        session.metadata["workflow_mode"] = "plan"
        loop.sessions.save(session)

        switch = await loop._process_message(
            InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="/build")
        )
        assert switch is not None
        assert "build 模式" in switch.content

        response = await loop._process_message(
            InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="帮我继续")
        )

        assert response is not None
        assert response.content == "mode-aware-reply"
        assert provider.calls == 1
        assert provider.last_tools != []


@pytest.mark.asyncio
async def test_bootstrap_turn_preserves_original_user_message_in_session_history(tmp_path: Path) -> None:
    provider = _BootstrapProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        channels_config=ChannelsConfig(
            feishu=FeishuConfig(
                onboarding_enabled=True,
                onboarding_blocking=False,
                onboarding_guide_once=True,
            )
        ),
        skillspec_config=SkillSpecConfig(enabled=True),
    )

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_bootstrap",
            chat_id="ou_bootstrap",
            content="你好",
            metadata={"chat_type": "p2p", "message_id": "m-1"},
        )
    )

    assert response is not None
    session = loop.sessions.get_or_create("feishu:ou_bootstrap")
    assert any(item.get("role") == "user" and item.get("content") == "你好" for item in session.messages)
    assert not any(
        item.get("role") == "user"
        and isinstance(item.get("content"), str)
        and item.get("content", "").startswith("[Bootstrap Internal Trigger]")
        for item in session.messages
    )

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
