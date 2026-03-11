import asyncio

import pytest

from nanobot.heartbeat.service import HeartbeatService, HeartbeatTarget
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class DummyProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]):
        super().__init__()
        self._responses = list(responses)
        self.calls = 0
        self.last_messages = None

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        self.last_messages = kwargs.get("messages")
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="", tool_calls=[])

    def get_default_model(self) -> str:
        return "test-model"


@pytest.mark.asyncio
async def test_start_is_idempotent(tmp_path) -> None:
    provider = DummyProvider([])

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        interval_s=9999,
        enabled=True,
    )

    await service.start()
    first_task = service._task
    await service.start()

    assert service._task is first_task

    service.stop()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_decide_returns_skip_when_no_tool_call(tmp_path) -> None:
    provider = DummyProvider([LLMResponse(content="no tool call", tool_calls=[])])
    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
    )

    action, tasks = await service._decide("heartbeat content")
    assert action == "skip"
    assert tasks == ""


@pytest.mark.asyncio
async def test_trigger_now_executes_when_decision_is_run(tmp_path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] do thing", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "check open tasks"},
                )
            ],
        )
    ])

    called_with: list[tuple[str, str]] = []

    async def _on_execute(target: HeartbeatTarget, tasks: str) -> str:
        called_with.append((target.session_key, tasks))
        return "done"

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
    )

    result = await service.trigger_now()
    assert result == "done"
    assert called_with == [("heartbeat", "check open tasks")]


@pytest.mark.asyncio
async def test_trigger_now_returns_none_when_decision_is_skip(tmp_path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] do thing", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "skip"},
                )
            ],
        )
    ])

    async def _on_execute(_target: HeartbeatTarget, tasks: str) -> str:
        return tasks

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
    )

    assert await service.trigger_now() is None


@pytest.mark.asyncio
async def test_decide_retries_transient_error_then_succeeds(tmp_path, monkeypatch) -> None:
    provider = DummyProvider([
        LLMResponse(content="429 rate limit", finish_reason="error"),
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "check open tasks"},
                )
            ],
        ),
    ])

    delays: list[int] = []

    async def _fake_sleep(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
    )

    action, tasks = await service._decide("heartbeat content")

    assert action == "run"
    assert tasks == "check open tasks"
    assert provider.calls == 2
    assert delays == [1]


@pytest.mark.asyncio
async def test_decision_context_includes_user_memory_and_history(tmp_path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] review follow-up items", encoding="utf-8")
    (tmp_path / "USER.md").write_text("- **昵称**：康哥\n", encoding="utf-8")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "MEMORY.md").write_text("Known preference: concise", encoding="utf-8")
    (tmp_path / "memory" / "HISTORY.md").write_text("[2026-03-11 10:00] promised a follow-up", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="hb_1", name="heartbeat", arguments={"action": "skip"})],
        )
    ])
    service = HeartbeatService(workspace=tmp_path, provider=provider, model="openai/gpt-4o-mini")

    await service.trigger_now()

    prompt = provider.last_messages[1]["content"]
    assert "## HEARTBEAT.md" in prompt
    assert "## USER.md" in prompt
    assert "康哥" in prompt
    assert "## memory/MEMORY.md" in prompt
    assert "Known preference: concise" in prompt
    assert "## memory/HISTORY.md (recent tail)" in prompt
    assert "promised a follow-up" in prompt


@pytest.mark.asyncio
async def test_tick_continues_when_one_target_fails(tmp_path) -> None:
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    (workspace_a / "HEARTBEAT.md").write_text("- [ ] do a", encoding="utf-8")
    (workspace_b / "HEARTBEAT.md").write_text("- [ ] do b", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(content="", tool_calls=[ToolCallRequest(id="hb_1", name="heartbeat", arguments={"action": "run", "tasks": "task-a"})]),
        LLMResponse(content="", tool_calls=[ToolCallRequest(id="hb_2", name="heartbeat", arguments={"action": "run", "tasks": "task-b"})]),
    ])

    executed: list[str] = []

    async def _on_execute(target: HeartbeatTarget, tasks: str) -> str:
        executed.append(f"{target.chat_id}:{tasks}")
        if target.chat_id == "chat-a":
            raise RuntimeError("boom")
        return "done-b"

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
        target_provider=lambda: [
            HeartbeatTarget(workspace_root=workspace_a, channel="feishu", chat_id="chat-a", session_key="a"),
            HeartbeatTarget(workspace_root=workspace_b, channel="feishu", chat_id="chat-b", session_key="b"),
        ],
    )

    await service._tick()

    assert executed == ["chat-a:task-a", "chat-b:task-b"]
