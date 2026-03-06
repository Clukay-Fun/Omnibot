import asyncio
import json

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import Tool
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import SkillSpecConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest


class _DummyProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    async def chat(self, **kwargs):
        self.calls += 1
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            merged = "\n".join(
                str(item.get("content") or "") for item in messages if isinstance(item, dict)
            )
            self.prompts.append(merged)
        return LLMResponse(content=f"llm-fallback-{self.calls}", tool_calls=[])

    def get_default_model(self) -> str:
        return "dummy"


class _FakeSearchTool(Tool):
    @property
    def name(self) -> str:
        return "bitable_search"

    @property
    def description(self) -> str:
        return "fake search"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return json.dumps(
            {
                "records": [
                    {"fields": {"title": "RowA"}},
                    {"fields": {"title": "RowB"}},
                ]
            },
            ensure_ascii=False,
        )


class _ScriptedProvider:
    def __init__(self, steps: list[str], *, timeout_sleep_seconds: float = 0.05) -> None:
        self.steps = steps
        self.timeout_sleep_seconds = timeout_sleep_seconds
        self.calls = 0
        self.prompts: list[str] = []

    async def chat(self, **kwargs):
        self.calls += 1
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            merged = "\n".join(
                str(item.get("content") or "") for item in messages if isinstance(item, dict)
            )
            self.prompts.append(merged)

        step = self.steps[self.calls - 1] if self.calls - 1 < len(self.steps) else "ok"
        if step == "timeout":
            await asyncio.sleep(self.timeout_sleep_seconds)
            return LLMResponse(content="timeout-late", tool_calls=[])
        if step == "tool_call":
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="call-1", name="bitable_search", arguments={"keyword": "x"})],
            )
        return LLMResponse(content=step, tool_calls=[])

    def get_default_model(self) -> str:
        return "dummy"


def _write_spec(workspace, filename: str, body: str) -> None:
    target = workspace / "skillspec" / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.strip() + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_loop_handles_skillspec_continuation_before_llm(tmp_path) -> None:
    _write_spec(
        tmp_path,
        "query_test.yaml",
        """
meta: {id: query_test, version: "0.1", description: 查询测试}
params: {type: object, properties: {query: {type: string}}}
action:
  kind: query
  table: {app_token: app_x, table_id: tbl_x}
response:
  output_policy:
    max_items: 1
error: {}
""",
    )
    provider = _DummyProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=True),
    )
    loop.tools.register(_FakeSearchTool())

    first = await loop._process_message(
        InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="/skill query_test 关键词")
    )
    assert first is not None
    assert first.content == "llm-fallback-1"
    assert first.metadata["skillspec_route"]["reason"] == "explicit"
    assert provider.calls == 1
    assert "RowA" in provider.prompts[0]

    second = await loop._process_message(
        InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="继续")
    )
    assert second is not None
    assert second.content == "llm-fallback-2"
    assert provider.calls == 2
    assert "RowB" in provider.prompts[1]


@pytest.mark.asyncio
async def test_loop_returns_no_more_continuation_without_falling_back_to_llm(tmp_path) -> None:
    provider = _DummyProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=True),
    )

    response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="oc_group", content="继续")
    )

    assert response is not None
    assert response.content == "没有可继续的内容了。"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_loop_falls_back_to_llm_when_skillspec_not_matched(tmp_path) -> None:
    provider = _DummyProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=True),
    )

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="普通聊天输入")
    )

    assert response is not None
    assert response.content == "llm-fallback-1"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_loop_routes_sensitive_skillspec_reply_to_sender_in_group(tmp_path) -> None:
    _write_spec(
        tmp_path,
        "sensitive_query.yaml",
        """
meta: {id: sensitive_query, version: "0.1", description: 敏感查询}
params: {type: object, properties: {query: {type: string}}}
action:
  kind: query
  table: {app_token: app_x, table_id: tbl_x}
response:
  sensitive: true
error: {}
""",
    )
    provider = _DummyProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=True),
    )
    loop.tools.register(_FakeSearchTool())

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_user",
            chat_id="oc_group",
            content="/skill sensitive_query something",
            metadata={"chat_type": "group", "message_id": "om_1", "thread_id": "omt_1"},
        )
    )

    assert response is not None
    assert response.chat_id == "ou_user"
    assert response.metadata["private_delivery"] is True
    assert "message_id" not in response.metadata


@pytest.mark.asyncio
async def test_skillspec_llm_render_retries_after_timeout(tmp_path) -> None:
    _write_spec(
        tmp_path,
        "query_test.yaml",
        """
meta: {id: query_test, version: "0.1", description: 查询测试}
params: {type: object, properties: {query: {type: string}}}
action:
  kind: query
  table: {app_token: app_x, table_id: tbl_x}
response:
  output_policy:
    max_items: 1
error: {}
""",
    )
    provider = _ScriptedProvider(["timeout", "retry-success"], timeout_sleep_seconds=0.2)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=True),
        llm_timeout_seconds=0.01,
    )
    loop.tools.register(_FakeSearchTool())

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="/skill query_test 关键词")
    )

    assert response is not None
    assert response.content == "retry-success"
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_skillspec_llm_render_retries_when_tool_calls_returned(tmp_path) -> None:
    _write_spec(
        tmp_path,
        "query_test.yaml",
        """
meta: {id: query_test, version: "0.1", description: 查询测试}
params: {type: object, properties: {query: {type: string}}}
action:
  kind: query
  table: {app_token: app_x, table_id: tbl_x}
response:
  output_policy:
    max_items: 1
error: {}
""",
    )
    provider = _ScriptedProvider(["tool_call", "retry-after-tool-call"])
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=True),
    )
    loop.tools.register(_FakeSearchTool())

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="/skill query_test 关键词")
    )

    assert response is not None
    assert response.content == "retry-after-tool-call"
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_skillspec_llm_render_falls_back_to_raw_after_retry_exhausted(tmp_path) -> None:
    _write_spec(
        tmp_path,
        "query_test.yaml",
        """
meta: {id: query_test, version: "0.1", description: 查询测试}
params: {type: object, properties: {query: {type: string}}}
action:
  kind: query
  table: {app_token: app_x, table_id: tbl_x}
response:
  output_policy:
    max_items: 1
error: {}
""",
    )
    provider = _ScriptedProvider(["timeout", "timeout"], timeout_sleep_seconds=0.2)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=True),
        llm_timeout_seconds=0.01,
    )
    loop.tools.register(_FakeSearchTool())

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="/skill query_test 关键词")
    )

    assert response is not None
    assert "RowA" in response.content
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_skillspec_llm_render_uses_configured_timeout_values(tmp_path) -> None:
    _write_spec(
        tmp_path,
        "query_test.yaml",
        """
meta: {id: query_test, version: "0.1", description: 查询测试}
params: {type: object, properties: {query: {type: string}}}
action:
  kind: query
  table: {app_token: app_x, table_id: tbl_x}
response:
  output_policy:
    max_items: 1
error: {}
""",
    )
    provider = _ScriptedProvider(["timeout", "configured-timeout-success"], timeout_sleep_seconds=0.2)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=True),
        llm_timeout_seconds=1.0,
        skillspec_render_primary_timeout_seconds=0.01,
        skillspec_render_retry_timeout_seconds=0.5,
    )
    loop.tools.register(_FakeSearchTool())

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="/skill query_test 关键词")
    )

    assert response is not None
    assert response.content == "configured-timeout-success"
    assert provider.calls == 2
