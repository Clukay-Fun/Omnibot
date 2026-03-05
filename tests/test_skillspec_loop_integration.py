import json

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import Tool
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import SkillSpecConfig
from nanobot.providers.base import LLMResponse


class _DummyProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        return LLMResponse(content="llm-fallback", tool_calls=[])

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
    assert "RowA" in first.content
    assert "继续" in first.content
    assert first.metadata["skillspec_route"]["reason"] == "explicit"
    assert provider.calls == 0

    second = await loop._process_message(
        InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="继续")
    )
    assert second is not None
    assert "RowB" in second.content
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
    assert response.content == "llm-fallback"
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
