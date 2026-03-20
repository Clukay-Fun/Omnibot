"""Test MemoryStore.consolidate() handles non-string tool call arguments.

Regression test for https://github.com/HKUDS/nanobot/issues/1042
When memory consolidation receives dict values instead of strings from the LLM
tool call response, it should serialize them to JSON instead of raising TypeError.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.memory import MemoryStore
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


def _make_session(message_count: int = 30, memory_window: int = 50):
    """Create a mock session with messages."""
    session = MagicMock()
    session.messages = [
        {"role": "user", "content": f"msg{i}", "timestamp": "2026-01-01 00:00"}
        for i in range(message_count)
    ]
    session.last_consolidated = 0
    return session


def _make_tool_response(history_entry, memory_update):
    """Create an LLMResponse with a save_memory tool call."""
    return LLMResponse(
        content=None,
        tool_calls=[
            ToolCallRequest(
                id="call_1",
                name="save_memory",
                arguments={
                    "history_entry": history_entry,
                    "memory_update": memory_update,
                },
            )
        ],
    )


class ScriptedProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]):
        super().__init__()
        self._responses = list(responses)
        self.calls = 0

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="", tool_calls=[])

    def get_default_model(self) -> str:
        return "test-model"


class TestMemoryConsolidationTypeHandling:
    """Test that consolidation handles various argument types correctly."""

    @pytest.mark.asyncio
    async def test_string_arguments_work(self, tmp_path: Path) -> None:
        """Normal case: LLM returns string arguments."""
        store = MemoryStore(tmp_path)
        provider = AsyncMock()
        provider.chat = AsyncMock(
            return_value=_make_tool_response(
                history_entry="[2026-01-01] User discussed testing.",
                memory_update="# Memory\nUser likes testing.",
            )
        )
        provider.chat_with_retry = provider.chat
        session = _make_session(message_count=60)

        result = await store.consolidate(session, provider, "test-model", memory_window=50)

        assert result is True
        assert store.history_file.exists()
        assert "[2026-01-01] User discussed testing." in store.history_file.read_text()
        assert "User likes testing." in store.memory_file.read_text()

    @pytest.mark.asyncio
    async def test_dict_arguments_serialized_to_json(self, tmp_path: Path) -> None:
        """Issue #1042: LLM returns dict instead of string — must not raise TypeError."""
        store = MemoryStore(tmp_path)
        provider = AsyncMock()
        provider.chat = AsyncMock(
            return_value=_make_tool_response(
                history_entry={"timestamp": "2026-01-01", "summary": "User discussed testing."},
                memory_update={"facts": ["User likes testing"], "topics": ["testing"]},
            )
        )
        provider.chat_with_retry = provider.chat
        session = _make_session(message_count=60)

        result = await store.consolidate(session, provider, "test-model", memory_window=50)

        assert result is True
        assert store.history_file.exists()
        history_content = store.history_file.read_text()
        parsed = json.loads(history_content.strip())
        assert parsed["summary"] == "User discussed testing."

        memory_content = store.memory_file.read_text()
        parsed_mem = json.loads(memory_content)
        assert "User likes testing" in parsed_mem["facts"]

    @pytest.mark.asyncio
    async def test_string_arguments_as_raw_json(self, tmp_path: Path) -> None:
        """Some providers return arguments as a JSON string instead of parsed dict."""
        store = MemoryStore(tmp_path)
        provider = AsyncMock()

        # Simulate arguments being a JSON string (not yet parsed)
        response = LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="call_1",
                    name="save_memory",
                    arguments=json.dumps({
                        "history_entry": "[2026-01-01] User discussed testing.",
                        "memory_update": "# Memory\nUser likes testing.",
                    }),
                )
            ],
        )
        provider.chat = AsyncMock(return_value=response)
        provider.chat_with_retry = provider.chat
        session = _make_session(message_count=60)

        result = await store.consolidate(session, provider, "test-model", memory_window=50)

        assert result is True
        assert "User discussed testing." in store.history_file.read_text()

    @pytest.mark.asyncio
    async def test_no_tool_call_returns_false(self, tmp_path: Path) -> None:
        """When LLM doesn't use the save_memory tool, return False."""
        store = MemoryStore(tmp_path)
        provider = AsyncMock()
        provider.chat = AsyncMock(
            return_value=LLMResponse(content="I summarized the conversation.", tool_calls=[])
        )
        provider.chat_with_retry = provider.chat
        session = _make_session(message_count=60)

        result = await store.consolidate(session, provider, "test-model", memory_window=50)

        assert result is False
        assert not store.history_file.exists()

    @pytest.mark.asyncio
    async def test_skips_when_few_messages(self, tmp_path: Path) -> None:
        """Consolidation should be a no-op when messages < keep_count."""
        store = MemoryStore(tmp_path)
        provider = AsyncMock()
        provider.chat_with_retry = provider.chat
        session = _make_session(message_count=10)

        result = await store.consolidate(session, provider, "test-model", memory_window=50)

        assert result is True
        provider.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_list_arguments_extracts_first_dict(self, tmp_path: Path) -> None:
        """Some providers return arguments as a list - extract first element if it's a dict."""
        store = MemoryStore(tmp_path)
        provider = AsyncMock()

        # Simulate arguments being a list containing a dict
        response = LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="call_1",
                    name="save_memory",
                    arguments=[{
                        "history_entry": "[2026-01-01] User discussed testing.",
                        "memory_update": "# Memory\nUser likes testing.",
                    }],
                )
            ],
        )
        provider.chat = AsyncMock(return_value=response)
        provider.chat_with_retry = provider.chat
        session = _make_session(message_count=60)

        result = await store.consolidate(session, provider, "test-model", memory_window=50)

        assert result is True
        assert "User discussed testing." in store.history_file.read_text()
        assert "User likes testing." in store.memory_file.read_text()

    @pytest.mark.asyncio
    async def test_list_arguments_empty_list_returns_false(self, tmp_path: Path) -> None:
        """Empty list arguments should return False."""
        store = MemoryStore(tmp_path)
        provider = AsyncMock()

        response = LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="call_1",
                    name="save_memory",
                    arguments=[],
                )
            ],
        )
        provider.chat = AsyncMock(return_value=response)
        provider.chat_with_retry = provider.chat
        session = _make_session(message_count=60)

        result = await store.consolidate(session, provider, "test-model", memory_window=50)

        assert result is False

    @pytest.mark.asyncio
    async def test_list_arguments_non_dict_content_returns_false(self, tmp_path: Path) -> None:
        """List with non-dict content should return False."""
        store = MemoryStore(tmp_path)
        provider = AsyncMock()

        response = LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="call_1",
                    name="save_memory",
                    arguments=["string", "content"],
                )
            ],
        )
        provider.chat = AsyncMock(return_value=response)
        provider.chat_with_retry = provider.chat
        session = _make_session(message_count=60)

        result = await store.consolidate(session, provider, "test-model", memory_window=50)

        assert result is False

    @pytest.mark.asyncio
    async def test_retries_transient_error_then_succeeds(self, tmp_path: Path, monkeypatch) -> None:
        store = MemoryStore(tmp_path)
        provider = ScriptedProvider([
            LLMResponse(content="503 server error", finish_reason="error"),
            _make_tool_response(
                history_entry="[2026-01-01] User discussed testing.",
                memory_update="# Memory\nUser likes testing.",
            ),
        ])
        session = _make_session(message_count=60)
        delays: list[int] = []

        async def _fake_sleep(delay: int) -> None:
            delays.append(delay)

        monkeypatch.setattr("nanobot.providers.base.asyncio.sleep", _fake_sleep)

        result = await store.consolidate(session, provider, "test-model", memory_window=50)

        assert result is True
        assert provider.calls == 2
        assert delays == [1]

    @pytest.mark.asyncio
    async def test_falls_back_to_auto_tool_choice_when_forced_call_is_unsupported(
        self, tmp_path: Path
    ) -> None:
        store = MemoryStore(tmp_path)
        provider = AsyncMock()
        provider.chat_with_retry = AsyncMock(
            side_effect=[
                LLMResponse(
                    content='Provider says tool_choice should be ["none", "auto"]',
                    finish_reason="error",
                ),
                _make_tool_response(
                    history_entry="[2026-01-01] User discussed testing.",
                    memory_update="# Memory\nUser likes testing.",
                ),
            ]
        )
        session = _make_session(message_count=60)

        result = await store.consolidate(
            session,
            provider,
            "test-model",
            memory_window=50,
            temperature=0.2,
            max_tokens=321,
            reasoning_effort="medium",
        )

        assert result is True
        assert provider.chat_with_retry.await_count == 2
        first_call = provider.chat_with_retry.await_args_list[0].kwargs
        second_call = provider.chat_with_retry.await_args_list[1].kwargs
        assert first_call["tool_choice"]["function"]["name"] == "save_memory"
        assert second_call["tool_choice"] == "auto"
        assert second_call["temperature"] == 0.2
        assert second_call["max_tokens"] == 321
        assert second_call["reasoning_effort"] == "medium"

    @pytest.mark.asyncio
    async def test_archive_messages_raw_archives_after_repeated_failures(self, tmp_path: Path) -> None:
        store = MemoryStore(tmp_path)
        provider = AsyncMock()
        provider.chat_with_retry = AsyncMock(
            return_value=LLMResponse(content="I summarized the conversation.", tool_calls=[])
        )

        result = await store.archive_messages(
            _make_session(message_count=8).messages,
            provider,
            "test-model",
        )

        assert result is True
        assert provider.chat_with_retry.await_count == store._MAX_FAILURES_BEFORE_RAW_ARCHIVE
        history = store.history_file.read_text(encoding="utf-8")
        assert "[RAW]" in history

    @pytest.mark.asyncio
    async def test_consolidation_prompt_mentions_worklog_boundary(self, tmp_path: Path) -> None:
        store = MemoryStore(tmp_path)
        (tmp_path / "WORKLOG.md").write_text(
            "## 进行中\n\n### 补 per-user worklog\n- 优先级：高\n- 状态/下一步：更新 prompt\n",
            encoding="utf-8",
        )
        provider = AsyncMock()
        provider.chat_with_retry = AsyncMock(
            return_value=_make_tool_response(
                history_entry="[2026-01-01] User discussed work tracking.",
                memory_update="# Memory\nUser is building a Feishu bot.",
            )
        )

        await store.consolidate(_make_session(message_count=60), provider, "test-model", memory_window=50)

        prompt = provider.chat_with_retry.await_args.kwargs["messages"][1]["content"]
        assert "## Current WORKLOG.md" in prompt
        assert "The user is building a Feishu bot" in prompt
        assert "Add per-user worklog support" in prompt
