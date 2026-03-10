import json
from typing import Any

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import Tool
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import SkillSpecConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest


class _CapturingProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.last_tool_names: list[str] = []

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        tools = kwargs.get("tools") or []
        self.last_tool_names = [
            str(tool.get("function", {}).get("name") or "")
            for tool in tools
            if isinstance(tool, dict)
        ]
        return LLMResponse(content=f"llm-fallback-{self.calls}", tool_calls=[])

    def get_default_model(self) -> str:
        return "dummy"


class _ToolCallingProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, **kwargs: Any) -> LLMResponse:
        _ = kwargs
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call-1",
                        name="bitable_prepare_create",
                        arguments={"request_text": "新增记录到工作表"},
                    )
                ],
            )
        return LLMResponse(content="llm-after-tool", tool_calls=[])

    def get_default_model(self) -> str:
        return "dummy"


class _FakeTableListTool(Tool):
    @property
    def name(self) -> str:
        return "bitable_list_tables"

    @property
    def description(self) -> str:
        return "fake bitable list tables"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs
        return json.dumps({"tables": []}, ensure_ascii=False)


class _FakeFieldListTool(Tool):
    @property
    def name(self) -> str:
        return "bitable_list_fields"

    @property
    def description(self) -> str:
        return "fake bitable list fields"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs
        return json.dumps({"fields": []}, ensure_ascii=False)


class _ExplodingSkillRuntime:
    def can_handle_continuation(self, text: str) -> bool:
        _ = text
        return False

    async def execute_if_matched(self, msg: InboundMessage, session: Any) -> Any:
        _ = (msg, session)
        raise AssertionError("ordinary messages should not call skillspec runtime")


class _FakePrepareCreateTool(Tool):
    @property
    def name(self) -> str:
        return "bitable_prepare_create"

    @property
    def description(self) -> str:
        return "fake prepare create"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs
        return json.dumps(
            {
                "request_text": "新增记录到工作表",
                "needs_table_confirmation": True,
                "candidates": [
                    {"table_id": "tbl_case", "name": "案件项目总库", "score": 2.1},
                    {"table_id": "tbl_week", "name": "团队周工作计划表", "score": 1.9},
                ],
            },
            ensure_ascii=False,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "tool"),
    [
        ("查找表格", _FakeTableListTool),
        ("列出字段", _FakeFieldListTool),
        ("找案件", None),
        ("查看团队周工作计划表所有内容", _FakeTableListTool),
    ],
)
async def test_ordinary_feishu_queries_bypass_pre_llm_routing(tmp_path, content, tool) -> None:
    provider = _CapturingProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=True),
    )
    if tool is not None:
        loop.tools.register(tool())

    loop._skillspec_runtime = _ExplodingSkillRuntime()  # type: ignore[assignment]

    response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content=content)
    )

    assert response is not None
    assert response.content == "llm-fallback-1"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_feishu_followup_overview_inherits_previous_query_context(tmp_path) -> None:
    provider = _CapturingProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=False),
    )
    loop.tools.register(_FakeTableListTool())

    session = loop.sessions.get_or_create("feishu:ou_chat")
    session.add_message("user", "查看团队周工作计划表所有内容")
    loop.sessions.save(session)

    response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="概览")
    )

    assert response is not None
    assert response.content == "llm-fallback-1"
    assert "bitable_list_tables" in provider.last_tool_names


@pytest.mark.asyncio
async def test_continuation_pages_recent_directory_hits_without_llm(tmp_path) -> None:
    provider = _CapturingProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=False),
    )
    session = loop.sessions.get_or_create("feishu:ou_chat")
    session.metadata["recent_directory_hits"] = [
        {"open_id": f"ou_{idx}", "display_name": f"联系人{idx}", "matched": {}}
        for idx in range(1, 8)
    ]
    session.metadata["result_selection"] = {
        "kind": "directory_contacts",
        "items": list(session.metadata["recent_directory_hits"]),
        "offset": 5,
        "page_size": 5,
    }
    loop.sessions.save(session)

    response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="继续")
    )

    assert response is not None
    assert "联系人6" in response.content
    assert "联系人7" in response.content
    assert loop.sessions.get_or_create("feishu:ou_chat").metadata["result_selection"]["offset"] == 7
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_continuation_keeps_global_selection_numbers_on_later_pages(tmp_path) -> None:
    provider = _CapturingProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=False),
    )
    session = loop.sessions.get_or_create("feishu:ou_chat")
    session.metadata["result_selection"] = {
        "kind": "table_candidates",
        "items": [{"table_id": f"tbl_{idx}", "name": f"候选表{idx}"} for idx in range(1, 8)],
        "offset": 5,
        "page_size": 5,
    }
    loop.sessions.save(session)

    response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="继续")
    )

    assert response is not None
    assert "- 6. 候选表6" in response.content
    assert "- 7. 候选表7" in response.content
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_prepare_create_ambiguous_result_returns_to_main_llm(tmp_path) -> None:
    provider = _ToolCallingProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=False),
    )
    loop.tools.register(_FakePrepareCreateTool())

    response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="帮我新增工作记录")
    )

    assert response is not None
    assert response.content == "llm-after-tool"
    session = loop.sessions.get_or_create("feishu:ou_chat")
    assert session.metadata.get("result_selection") in ({}, None)
