from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory import MemoryStore
from nanobot.agent.overlay import OverlayContext
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import ToolCallRequest
from nanobot.session.manager import Session


def _make_loop(tmp_path):
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    bus = MessageBus()

    with patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    return loop


@pytest.mark.asyncio
async def test_consolidate_memory_uses_overlay_root_from_session_metadata(tmp_path) -> None:
    loop = _make_loop(tmp_path)
    overlay_root = tmp_path / "users" / "feishu" / "tenant-1" / "ou_user_1"
    (overlay_root / "memory").mkdir(parents=True)

    session = Session(key="feishu:dm:ou_user_1")
    session.metadata = OverlayContext(
        system_overlay_root=str(overlay_root),
        system_overlay_bootstrap=True,
    ).to_metadata()

    captured = {}

    async def _fake_consolidate(
        self,
        _session,
        _provider,
        _model,
        *,
        archive_all=False,
        memory_window=50,
        temperature=0.1,
        max_tokens=4096,
        reasoning_effort=None,
        purpose=None,
    ):
        captured["memory_dir"] = self.memory_dir
        captured["archive_all"] = archive_all
        captured["memory_window"] = memory_window
        captured["temperature"] = temperature
        captured["max_tokens"] = max_tokens
        captured["reasoning_effort"] = reasoning_effort
        captured["purpose"] = purpose
        return True

    with patch.object(MemoryStore, "consolidate", _fake_consolidate):
        result = await loop._consolidate_memory(session)

    assert result is True
    assert captured["memory_dir"] == overlay_root / "memory"
    assert captured["temperature"] == loop.temperature
    assert captured["max_tokens"] == loop.max_tokens
    assert captured["reasoning_effort"] == loop.reasoning_effort
    assert captured["purpose"] == "memory_consolidation"


@pytest.mark.asyncio
async def test_process_message_persists_overlay_context_before_failure(tmp_path) -> None:
    loop = _make_loop(tmp_path)
    overlay_root = tmp_path / "users" / "feishu" / "tenant-1" / "ou_user_1"
    overlay_root.mkdir(parents=True)

    msg = InboundMessage(
        channel="feishu",
        sender_id="ou_user_1",
        chat_id="ou_user_1",
        content="hello",
        metadata=OverlayContext(
            system_overlay_root=str(overlay_root),
            system_overlay_bootstrap=True,
        ).to_metadata(),
        session_key_override="feishu:dm:ou_user_1",
    )

    loop._run_agent_loop = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="boom"):
        await loop._process_message(msg)

    session = loop.sessions.get_or_create("feishu:dm:ou_user_1")
    overlay = OverlayContext.from_metadata(session.metadata)
    assert overlay.system_overlay_root == str(overlay_root)
    assert overlay.system_overlay_bootstrap is True


@pytest.mark.asyncio
async def test_process_message_marks_tool_progress_in_outbound_metadata(tmp_path) -> None:
    loop = _make_loop(tmp_path)

    async def _fake_run_agent_loop(_messages, on_progress=None, tool_registry=None):
        assert on_progress is not None
        assert tool_registry is loop.tools
        await on_progress('web_search("测试查询")', tool_hint=True)
        return "done", [], []

    loop._run_agent_loop = _fake_run_agent_loop  # type: ignore[method-assign]

    msg = InboundMessage(
        channel="feishu",
        sender_id="ou_user_1",
        chat_id="ou_user_1",
        content="hello",
    )

    result = await loop._process_message(msg)

    progress = await loop.bus.consume_outbound()
    assert progress.metadata["_progress"] is True
    assert progress.metadata["_tool_hint"] is True
    assert progress.metadata["_is_tool_progress"] is True
    assert result is not None
    assert result.content == "done"


def test_tool_hint_keeps_full_argument_for_downstream_progress_mapping(tmp_path) -> None:
    loop = _make_loop(tmp_path)
    hint = loop._tool_hint([
        ToolCallRequest(
            id="call_1",
            name="read_file",
            arguments={"path": "/Users/clukay/Program/ominibot/nanobot/skills/feishu-workspace/references/bitable.md"},
        )
    ])

    assert hint == 'read_file("/Users/clukay/Program/ominibot/nanobot/skills/feishu-workspace/references/bitable.md")'
