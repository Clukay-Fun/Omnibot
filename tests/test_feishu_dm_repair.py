from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.overlay import OverlayContext
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class QueueProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]):
        super().__init__()
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.calls.append(
            {
                "messages": kwargs.get("messages"),
                "tools": kwargs.get("tools"),
                "purpose": kwargs.get("purpose"),
            }
        )
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="NO_OP", tool_calls=[])

    def get_default_model(self) -> str:
        return "test-model"


def _make_loop(tmp_path: Path, provider: QueueProvider) -> AgentLoop:
    bus = MessageBus()
    with patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    return loop


def _make_overlay(tmp_path: Path) -> Path:
    overlay = tmp_path / "users" / "feishu" / "tenant-1" / "ou_user_1"
    (overlay / "memory").mkdir(parents=True)
    (overlay / "USER.md").write_text(
        "# USER.md - 用户档案\n\n- **昵称**：(待了解)\n- **称呼方式**：(待了解)\n- **长期工作背景**：(待了解)\n- **表达风格偏好**：(待了解)\n",
        encoding="utf-8",
    )
    (overlay / "WORKLOG.md").write_text(
        "# WORKLOG.md - 当前工作面板\n\n## 进行中\n\n## 待处理\n\n## 已完成\n",
        encoding="utf-8",
    )
    (overlay / "memory" / "MEMORY.md").write_text("# MEMORY.md - 长期记忆\n\n", encoding="utf-8")
    (overlay / "BOOTSTRAP.md").write_text("bootstrap active", encoding="utf-8")
    return overlay


def _make_msg(overlay: Path, content: str) -> InboundMessage:
    return InboundMessage(
        channel="feishu",
        sender_id="ou_user_1",
        chat_id="ou_user_1",
        content=content,
        metadata=OverlayContext(
            system_overlay_root=str(overlay),
            system_overlay_bootstrap=True,
        ).to_metadata({"chat_type": "p2p", "turn_id": "feishu-turn-1"}),
        session_key_override="feishu:dm:ou_user_1",
    )


async def _drain_repair_tasks(loop: AgentLoop) -> None:
    if loop._feishu_repair_tasks:
        await asyncio.gather(*list(loop._feishu_repair_tasks), return_exceptions=False)


@pytest.mark.asyncio
async def test_feishu_dm_repair_backfills_user_file_when_main_turn_misses_it(tmp_path: Path) -> None:
    overlay = _make_overlay(tmp_path)
    updated_user = (
        "# USER.md - 用户档案\n\n"
        "- **昵称**：(待了解)\n"
        "- **称呼方式**：小敬\n"
        "- **长期工作背景**：(待了解)\n"
        "- **表达风格偏好**：结论先行，少铺垫\n"
    )
    provider = QueueProvider(
        [
            LLMResponse(content="已记住。", tool_calls=[]),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="repair_1",
                        name="write_file",
                        arguments={"path": "USER.md", "content": updated_user},
                    )
                ],
            ),
            LLMResponse(content="REPAIRED", tool_calls=[]),
        ]
    )
    loop = _make_loop(tmp_path, provider)

    response = await loop._process_message(_make_msg(overlay, "记住，我以后希望你结论先行，少一点铺垫。"))
    await _drain_repair_tasks(loop)

    assert response is not None
    assert response.content == "已记住。"
    assert (overlay / "USER.md").read_text(encoding="utf-8") == updated_user
    assert provider.calls[-1]["purpose"] == "feishu_dm_post_turn_repair"


@pytest.mark.asyncio
async def test_feishu_dm_repair_noops_for_ack_turns(tmp_path: Path) -> None:
    overlay = _make_overlay(tmp_path)
    before_user = (overlay / "USER.md").read_text(encoding="utf-8")
    before_worklog = (overlay / "WORKLOG.md").read_text(encoding="utf-8")
    before_memory = (overlay / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    provider = QueueProvider(
        [
            LLMResponse(content="收到。", tool_calls=[]),
            LLMResponse(content="NO_OP", tool_calls=[]),
        ]
    )
    loop = _make_loop(tmp_path, provider)

    response = await loop._process_message(_make_msg(overlay, "是的"))
    await _drain_repair_tasks(loop)

    assert response is not None
    assert response.content == "收到。"
    assert (overlay / "USER.md").read_text(encoding="utf-8") == before_user
    assert (overlay / "WORKLOG.md").read_text(encoding="utf-8") == before_worklog
    assert (overlay / "memory" / "MEMORY.md").read_text(encoding="utf-8") == before_memory


@pytest.mark.asyncio
async def test_feishu_dm_repair_noops_when_main_turn_already_persisted(tmp_path: Path) -> None:
    overlay = _make_overlay(tmp_path)
    updated_user = (
        "# USER.md - 用户档案\n\n"
        "- **昵称**：小敬\n"
        "- **称呼方式**：小敬\n"
        "- **长期工作背景**：(待了解)\n"
        "- **表达风格偏好**：(待了解)\n"
    )
    provider = QueueProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="main_write_1",
                        name="write_file",
                        arguments={"path": str(overlay / "USER.md"), "content": updated_user},
                    )
                ],
            ),
            LLMResponse(content="已记住，小敬。", tool_calls=[]),
            LLMResponse(content="NO_OP", tool_calls=[]),
        ]
    )
    loop = _make_loop(tmp_path, provider)

    response = await loop._process_message(_make_msg(overlay, "我是小敬"))
    await _drain_repair_tasks(loop)

    assert response is not None
    assert response.content == "已记住，小敬。"
    assert (overlay / "USER.md").read_text(encoding="utf-8") == updated_user


@pytest.mark.asyncio
async def test_feishu_dm_repair_registry_only_allows_target_files(tmp_path: Path) -> None:
    overlay = _make_overlay(tmp_path)
    provider = QueueProvider([])
    loop = _make_loop(tmp_path, provider)
    registry = loop._build_feishu_repair_tool_registry(overlay)

    result = await registry.execute(
        "write_file",
        {"path": "HEARTBEAT.md", "content": "should fail"},
    )

    assert "outside the Feishu DM repair allowlist" in result


@pytest.mark.asyncio
async def test_feishu_dm_new_user_replay_updates_user_worklog_and_memory(tmp_path: Path) -> None:
    overlay = _make_overlay(tmp_path)
    provider = QueueProvider(
        [
            LLMResponse(content="你好，小敬。", tool_calls=[]),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="r1",
                        name="write_file",
                        arguments={
                            "path": "USER.md",
                            "content": (
                                "# USER.md - 用户档案\n\n"
                                "- **昵称**：小敬\n"
                                "- **称呼方式**：小敬\n"
                                "- **长期工作背景**：(待了解)\n"
                                "- **表达风格偏好**：(待了解)\n"
                            ),
                        },
                    )
                ],
            ),
            LLMResponse(content="REPAIRED", tool_calls=[]),
            LLMResponse(content="好，我会更直接。", tool_calls=[]),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="r2",
                        name="write_file",
                        arguments={
                            "path": "USER.md",
                            "content": (
                                "# USER.md - 用户档案\n\n"
                                "- **昵称**：小敬\n"
                                "- **称呼方式**：小敬\n"
                                "- **长期工作背景**：(待了解)\n"
                                "- **表达风格偏好**：结论先行，简短且简洁\n"
                            ),
                        },
                    )
                ],
            ),
            LLMResponse(content="REPAIRED", tool_calls=[]),
            LLMResponse(content="记住了。", tool_calls=[]),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="r3",
                        name="write_file",
                        arguments={
                            "path": "memory/MEMORY.md",
                            "content": "# MEMORY.md - 长期记忆\n\n- 用户长期在做 Feishu bot 和 nanobot 相关工作。\n",
                        },
                    )
                ],
            ),
            LLMResponse(content="REPAIRED", tool_calls=[]),
            LLMResponse(content="明白，先做对话测试。", tool_calls=[]),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="r4",
                        name="write_file",
                        arguments={
                            "path": "WORKLOG.md",
                            "content": (
                                "# WORKLOG.md - 当前工作面板\n\n"
                                "## 进行中\n\n"
                                "### Feishu 体验链路优化\n"
                                "- 优先级：高\n"
                                "- 状态/下一步：本轮交付 feishu 对话测试\n\n"
                                "## 待处理\n\n"
                                "## 已完成\n"
                            ),
                        },
                    )
                ],
            ),
            LLMResponse(content="REPAIRED", tool_calls=[]),
        ]
    )
    loop = _make_loop(tmp_path, provider)

    for content in (
        "我是小敬",
        "结论先行，还是简洁一点吧。",
        "记住，我这段时间长期都在做 Feishu bot 和 nanobot 相关工作。",
        "上一个节点：Feishu 体验链路优化\n本轮要交付：feishu 对话测试",
    ):
        await loop._process_message(_make_msg(overlay, content))
        await _drain_repair_tasks(loop)

    user_text = (overlay / "USER.md").read_text(encoding="utf-8")
    worklog_text = (overlay / "WORKLOG.md").read_text(encoding="utf-8")
    memory_text = (overlay / "memory" / "MEMORY.md").read_text(encoding="utf-8")

    assert "小敬" in user_text
    assert "结论先行" in user_text
    assert "简短且简洁" in user_text
    assert "Feishu 体验链路优化" in worklog_text
    assert "feishu 对话测试" in worklog_text
    assert "Feishu bot 和 nanobot" in memory_text


@pytest.mark.asyncio
async def test_feishu_placeholder_reply_logs_separately_from_streamer(tmp_path: Path) -> None:
    overlay = _make_overlay(tmp_path)
    provider = QueueProvider(
        [
            LLMResponse(content="正在处理，稍后给你结果。", tool_calls=[]),
            LLMResponse(content="NO_OP", tool_calls=[]),
        ]
    )

    with patch("nanobot.agent.loop.logger") as mock_logger:
        mock_logger.bind.return_value = MagicMock()
        loop = _make_loop(tmp_path, provider)
        await loop._process_message(_make_msg(overlay, "帮我续上"))
        await _drain_repair_tasks(loop)

    assert any(
        call.kwargs.get("event") == "feishu_model_placeholder_reply"
        for call in mock_logger.bind.call_args_list
    )
