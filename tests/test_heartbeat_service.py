import asyncio
import json

import pytest

from nanobot.heartbeat.service import HeartbeatService, HeartbeatTarget
from nanobot.heartbeat.types import HeartbeatExecutionError, HeartbeatExecutionResult
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


def _log_files(root):
    return sorted((root / "memory" / "heartbeat-logs").glob("*.jsonl"))


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

    action, tasks, summary = await service._decide("heartbeat content")
    assert action == "skip"
    assert tasks == ""
    assert summary


@pytest.mark.asyncio
async def test_trigger_now_executes_when_decision_is_run(tmp_path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] do thing\n", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={
                        "action": "run",
                        "tasks": "check open tasks",
                        "summary": "Found an active follow-up to execute.",
                    },
                )
            ],
        )
    ])

    called_with: list[tuple[str, str]] = []

    async def _on_execute(target: HeartbeatTarget, tasks: str) -> HeartbeatExecutionResult:
        called_with.append((target.session_key, tasks))
        return HeartbeatExecutionResult(
            response_text="done",
            state_summary="Checked open tasks and notified the user.",
            transcript_messages=[
                {"role": "user", "content": "check open tasks", "timestamp": "2026-03-18T10:00:00"},
                {"role": "assistant", "content": "done", "timestamp": "2026-03-18T10:00:01"},
            ],
        )

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
    )

    result = await service.trigger_now()

    assert result == "done"
    assert called_with == [("heartbeat", "check open tasks")]
    heartbeat_text = (tmp_path / "HEARTBEAT.md").read_text(encoding="utf-8")
    assert "Decision: run" in heartbeat_text
    assert "Checked open tasks and notified the user." in heartbeat_text
    logs = _log_files(tmp_path)
    assert len(logs) == 1
    metadata = json.loads(logs[0].read_text(encoding="utf-8").splitlines()[0])
    assert metadata["metadata"]["heartbeat"]["decision"] == "run"


@pytest.mark.asyncio
async def test_trigger_now_returns_none_when_decision_is_skip_and_updates_state(tmp_path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] do thing\n", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "skip", "summary": "No active follow-up remains."},
                )
            ],
        )
    ])

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
    )

    assert await service.trigger_now() is None
    heartbeat_text = (tmp_path / "HEARTBEAT.md").read_text(encoding="utf-8")
    assert "Decision: skip" in heartbeat_text
    assert "No active follow-up remains." in heartbeat_text
    assert _log_files(tmp_path) == []


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
                    arguments={
                        "action": "run",
                        "tasks": "check open tasks",
                        "summary": "A follow-up task still needs work.",
                    },
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

    action, tasks, summary = await service._decide("heartbeat content")

    assert action == "run"
    assert tasks == "check open tasks"
    assert summary == "A follow-up task still needs work."
    assert provider.calls == 2
    assert delays == [1]


@pytest.mark.asyncio
async def test_decision_context_includes_user_memory_but_not_history(tmp_path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] review follow-up items", encoding="utf-8")
    (tmp_path / "WORKLOG.md").write_text(
        "## 进行中\n\n### 补 worklog snapshot\n- 优先级：高\n- 状态/下一步：更新 context builder\n",
        encoding="utf-8",
    )
    (tmp_path / "USER.md").write_text("- **昵称**：康哥\n", encoding="utf-8")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "MEMORY.md").write_text("Known preference: concise", encoding="utf-8")
    (tmp_path / "memory" / "HISTORY.md").write_text("[2026-03-11 10:00] promised a follow-up", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "skip", "summary": "Nothing to do right now."},
                )
            ],
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
    assert "## WORKLOG.md" in prompt
    assert "补 worklog snapshot" in prompt
    assert "memory/HISTORY.md" not in prompt
    assert "promised a follow-up" not in prompt


@pytest.mark.asyncio
async def test_failed_execution_updates_state_and_writes_audit_log(tmp_path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] do thing\n", encoding="utf-8")
    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={
                        "action": "run",
                        "tasks": "check open tasks",
                        "summary": "A follow-up task still needs work.",
                    },
                )
            ],
        )
    ])

    async def _on_execute(_target: HeartbeatTarget, _tasks: str) -> HeartbeatExecutionResult:
        raise HeartbeatExecutionError(
            "Execution failed: missing auth.",
            transcript_messages=[
                {"role": "user", "content": "check open tasks", "timestamp": "2026-03-18T10:00:00"},
            ],
        )

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
    )

    assert await service.trigger_now() is None
    heartbeat_text = (tmp_path / "HEARTBEAT.md").read_text(encoding="utf-8")
    assert "Decision: failed" in heartbeat_text
    assert "Execution failed: missing auth." in heartbeat_text
    logs = _log_files(tmp_path)
    assert len(logs) == 1
    metadata = json.loads(logs[0].read_text(encoding="utf-8").splitlines()[0])
    assert metadata["metadata"]["heartbeat"]["decision"] == "failed"


def test_upsert_state_block_preserves_tasks_and_repairs_malformed_markers(tmp_path) -> None:
    heartbeat_file = tmp_path / "HEARTBEAT.md"
    heartbeat_file.write_text(
        "- [ ] keep this task\n"
        "<!-- HEARTBEAT_STATE:BEGIN -->\n"
        "## Last Heartbeat Run\n"
        "- At: old\n"
        "- Decision: skip\n"
        "- Summary: stale\n"
        "<!-- HEARTBEAT_STATE:BEGIN -->\n"
        "- [ ] another task\n",
        encoding="utf-8",
    )
    service = HeartbeatService(workspace=tmp_path, provider=DummyProvider([]), model="test-model")
    target = HeartbeatTarget(
        workspace_root=tmp_path,
        channel="cli",
        chat_id="direct",
        session_key="heartbeat",
    )

    service._upsert_state_block(target, "skip", "Fresh summary.")

    updated = heartbeat_file.read_text(encoding="utf-8")
    assert "- [ ] keep this task" in updated
    assert "- [ ] another task" in updated
    assert updated.count("<!-- HEARTBEAT_STATE:BEGIN -->") == 1
    assert updated.count("<!-- HEARTBEAT_STATE:END -->") == 1
    assert "Fresh summary." in updated


def test_audit_log_retention_prunes_older_files(tmp_path) -> None:
    (tmp_path / "memory" / "heartbeat-logs").mkdir(parents=True)
    service = HeartbeatService(workspace=tmp_path, provider=DummyProvider([]), model="test-model")
    target = HeartbeatTarget(
        workspace_root=tmp_path,
        channel="cli",
        chat_id="direct",
        session_key="heartbeat",
    )

    for i in range(35):
        service._write_audit_log(
            target,
            decision="run",
            summary=f"summary {i}",
            transcript_messages=[{"role": "assistant", "content": f"log {i}"}],
        )

    logs = _log_files(tmp_path)
    assert len(logs) == 30


@pytest.mark.asyncio
async def test_tick_continues_when_one_target_fails(tmp_path) -> None:
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    (workspace_a / "HEARTBEAT.md").write_text("- [ ] do a", encoding="utf-8")
    (workspace_b / "HEARTBEAT.md").write_text("- [ ] do b", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "task-a", "summary": "run a"},
                )
            ],
        ),
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_2",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "task-b", "summary": "run b"},
                )
            ],
        ),
    ])

    executed: list[str] = []

    async def _on_execute(target: HeartbeatTarget, tasks: str) -> HeartbeatExecutionResult:
        executed.append(f"{target.chat_id}:{tasks}")
        if target.chat_id == "chat-a":
            raise HeartbeatExecutionError("boom")
        return HeartbeatExecutionResult(
            response_text="done-b",
            state_summary="Completed task-b",
            transcript_messages=[{"role": "assistant", "content": "done-b"}],
        )

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
