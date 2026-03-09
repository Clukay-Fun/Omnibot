import json
from typing import Any

import pytest

from nanobot.agent.coordinators.base import AgentCoordinator
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import Tool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import SkillSpecConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest


class _ScriptedProvider:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self.calls = 0
        self.prompts: list[str] = []

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            self.prompts.append("\n".join(str(item.get("content") or "") for item in messages if isinstance(item, dict)))
        if self.calls > len(self._responses):
            return LLMResponse(content=f"unexpected-{self.calls}", tool_calls=[])
        return self._responses[self.calls - 1]

    def get_default_model(self) -> str:
        return "dummy"


class _FakeCreateTool(Tool):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "bitable_create"

    @property
    def description(self) -> str:
        return "fake bitable create"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"fields": {"type": "object"}}, "required": ["fields"]}

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        if kwargs.get("confirm_token"):
            return json.dumps({"success": True, "record_id": "rec-created"}, ensure_ascii=False)
        return json.dumps(
            {
                "dry_run": True,
                "preview": {
                    "action": "create",
                    "table_id": kwargs.get("table_id", "tbl_default"),
                    "fields": kwargs.get("fields", {}),
                },
                "confirm_token": "tok-create-1",
            },
            ensure_ascii=False,
        )


class _FakeUpdateTool(Tool):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "bitable_update"

    @property
    def description(self) -> str:
        return "fake bitable update"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"record_id": {"type": "string"}, "fields": {"type": "object"}},
            "required": ["record_id", "fields"],
        }

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        if kwargs.get("confirm_token"):
            return json.dumps({"success": True, "record_id": kwargs.get("record_id", "rec-updated")}, ensure_ascii=False)
        return json.dumps(
            {
                "dry_run": True,
                "preview": {
                    "action": "update",
                    "table_id": kwargs.get("table_id", "tbl_default"),
                    "record_id": kwargs.get("record_id"),
                    "fields": kwargs.get("fields", {}),
                },
                "confirm_token": "tok-update-1",
            },
            ensure_ascii=False,
        )


class _FakePrepareCreateTool(Tool):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "bitable_prepare_create"

    @property
    def description(self) -> str:
        return "fake prepare create"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"request_text": {"type": "string"}}, "required": ["request_text"]}

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        return json.dumps(self.payload, ensure_ascii=False)


class _FakeFieldSchemaTool(Tool):
    @property
    def name(self) -> str:
        return "bitable_list_fields"

    @property
    def description(self) -> str:
        return "fake field schema"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs
        return json.dumps(
            {
                "fields": [
                    {"field_name": "日期", "type": 5, "property": {}},
                    {"field_name": "人员", "type": 11, "property": {}},
                    {"field_name": "未完成事项", "type": 1, "property": {}},
                ]
            },
            ensure_ascii=False,
        )


class _FakeDirectorySearchTool(Tool):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "bitable_directory_search"

    @property
    def description(self) -> str:
        return "fake directory search"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        contacts = [
            {
                "open_id": "ou_fang",
                "display_name": "房怡康",
                "matched": {"姓名": "房怡康", "邮箱": "fang@example.com"},
            },
            {
                "open_id": "ou_zhang",
                "display_name": "张三",
                "matched": {"姓名": "张三", "邮箱": "zhangsan@example.com"},
            },
        ]
        keyword = str(kwargs.get("keyword") or "").strip()
        if keyword:
            contacts = [item for item in contacts if keyword in item["display_name"]]
        return json.dumps({"keyword": keyword, "contacts": contacts, "total": len(contacts)}, ensure_ascii=False)


def _build_loop(tmp_path, provider: _ScriptedProvider) -> AgentLoop:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=False),
    )
    return loop


class _CoordinatorOnlyProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, **kwargs: Any) -> LLMResponse:
        _ = kwargs
        self.calls += 1
        return LLMResponse(content="llm-fallback", tool_calls=[])

    def get_default_model(self) -> str:
        return "dummy"


class _StubCoordinator(AgentCoordinator):
    async def handle(self, *, msg: InboundMessage, session) -> OutboundMessage | None:  # type: ignore[override]
        _ = session
        if msg.content == "coordinator first":
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="handled by coordinator")
        return None


@pytest.mark.asyncio
async def test_loop_runs_registered_coordinator_before_llm(tmp_path) -> None:
    provider = _CoordinatorOnlyProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=False),
    )
    loop._coordinators.insert(0, _StubCoordinator())

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="coordinator first")
    )

    assert response is not None
    assert response.content == "handled by coordinator"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_contact_query_coordinator_short_circuits_directory_list_without_llm(tmp_path) -> None:
    provider = _CoordinatorOnlyProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=False),
    )
    directory_tool = _FakeDirectorySearchTool()
    loop.tools.register(directory_tool)

    response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="通讯录里都有谁")
    )

    assert response is not None
    assert "房怡康" in response.content
    assert provider.calls == 0
    assert directory_tool.calls == [{"limit": 10}]


@pytest.mark.asyncio
async def test_contact_query_coordinator_short_circuits_named_lookup_without_llm(tmp_path) -> None:
    provider = _CoordinatorOnlyProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=False),
    )
    directory_tool = _FakeDirectorySearchTool()
    loop.tools.register(directory_tool)

    response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="查房怡康")
    )

    assert response is not None
    assert "ou_fang" in response.content
    assert provider.calls == 0
    assert directory_tool.calls == [{"keyword": "房怡康", "limit": 5}]


@pytest.mark.asyncio
async def test_normal_chat_write_dry_run_short_circuits_into_pending_confirmation(tmp_path) -> None:
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call-1",
                        name="bitable_create",
                        arguments={"table_id": "tbl_week", "fields": {"日期": "2026-03-08"}},
                    )
                ],
            )
        ]
    )
    loop = _build_loop(tmp_path, provider)
    create_tool = _FakeCreateTool()
    loop.tools.register(create_tool)

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="帮我新增一条工作记录")
    )

    assert response is not None
    assert "确认" in response.content
    assert provider.calls == 1
    assert len(create_tool.calls) == 1

    session = loop.sessions.get_or_create("cli:chat")
    pending = session.metadata.get("pending_write") or {}
    assert pending["tool"] == "bitable_create"
    assert pending["token"] == "tok-create-1"
    assert pending["args"]["table_id"] == "tbl_week"


@pytest.mark.asyncio
async def test_normal_chat_confirm_reuses_saved_payload_without_llm(tmp_path) -> None:
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call-1",
                        name="bitable_create",
                        arguments={"table_id": "tbl_week", "fields": {"日期": "2026-03-08"}},
                    )
                ],
            )
        ]
    )
    loop = _build_loop(tmp_path, provider)
    create_tool = _FakeCreateTool()
    loop.tools.register(create_tool)

    _ = await loop._process_message(
        InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="帮我新增一条工作记录")
    )
    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="确认")
    )

    assert response is not None
    assert "rec-created" in response.content
    assert provider.calls == 1
    assert len(create_tool.calls) == 2
    assert create_tool.calls[1]["confirm_token"] == "tok-create-1"
    assert create_tool.calls[1]["table_id"] == "tbl_week"

    session = loop.sessions.get_or_create("cli:chat")
    assert session.metadata.get("pending_write") in ({}, None)


@pytest.mark.asyncio
async def test_prepare_create_auto_executes_suggested_update_without_second_llm_turn(tmp_path) -> None:
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call-1",
                        name="bitable_prepare_create",
                        arguments={"request_text": "帮我给房怡康补一下本周工作计划：整理合同台账"},
                    )
                ],
            )
        ]
    )
    loop = _build_loop(tmp_path, provider)
    prepare_tool = _FakePrepareCreateTool(
        {
            "needs_table_confirmation": False,
            "selected_table": {"table_id": "tbl_week", "name": "团队周工作计划表"},
            "draft_fields": {"姓名": "房怡康", "周次": "本周", "工作内容": "整理合同台账"},
            "identity_strategy": ["姓名", "周次"],
            "record_lookup": {"attempted": True, "matched": 1, "records": [{"record_id": "rec_week_1"}]},
            "operation_guess": "update_existing",
            "needs_record_confirmation": False,
            "next_step": {
                "tool": "bitable_update",
                "mode": "dry_run",
                "arguments": {"table_id": "tbl_week", "record_id": "rec_week_1", "fields": {"工作内容": "整理合同台账"}},
            },
        }
    )
    update_tool = _FakeUpdateTool()
    loop.tools.register(prepare_tool)
    loop.tools.register(update_tool)

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="帮我给房怡康补一下本周工作计划：整理合同台账")
    )

    assert response is not None
    assert "确认" in response.content
    assert provider.calls == 1
    assert prepare_tool.calls == [{"request_text": "帮我给房怡康补一下本周工作计划：整理合同台账"}]
    assert len(update_tool.calls) == 1
    assert update_tool.calls[0]["record_id"] == "rec_week_1"
    assert update_tool.calls[0]["fields"] == {"工作内容": "整理合同台账"}

    session = loop.sessions.get_or_create("cli:chat")
    pending = session.metadata.get("pending_write") or {}
    assert pending["tool"] == "bitable_update"
    assert pending["token"] == "tok-update-1"
    assert pending["args"]["record_id"] == "rec_week_1"


@pytest.mark.asyncio
async def test_prepare_create_auto_executes_suggested_create_without_second_llm_turn(tmp_path) -> None:
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call-1",
                        name="bitable_prepare_create",
                        arguments={"request_text": "新增合同，合同编号 HT-001"},
                    )
                ],
            )
        ]
    )
    loop = _build_loop(tmp_path, provider)
    prepare_tool = _FakePrepareCreateTool(
        {
            "needs_table_confirmation": False,
            "selected_table": {"table_id": "tbl_contract", "name": "合同管理"},
            "draft_fields": {"合同编号": "HT-001"},
            "identity_strategy": ["合同编号"],
            "record_lookup": {"attempted": True, "matched": 0, "records": []},
            "operation_guess": "create_new",
            "needs_record_confirmation": False,
            "next_step": {
                "tool": "bitable_create",
                "mode": "dry_run",
                "arguments": {"table_id": "tbl_contract", "fields": {"合同编号": "HT-001"}},
            },
        }
    )
    create_tool = _FakeCreateTool()
    loop.tools.register(prepare_tool)
    loop.tools.register(create_tool)

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="新增合同，合同编号 HT-001")
    )

    assert response is not None
    assert "确认" in response.content
    assert provider.calls == 1
    assert len(create_tool.calls) == 1
    assert create_tool.calls[0]["table_id"] == "tbl_contract"
    assert create_tool.calls[0]["fields"] == {"合同编号": "HT-001"}

    session = loop.sessions.get_or_create("cli:chat")
    pending = session.metadata.get("pending_write") or {}
    assert pending["tool"] == "bitable_create"
    assert pending["token"] == "tok-create-1"


@pytest.mark.asyncio
async def test_normal_chat_confirmation_with_extra_fields_refreshes_preview(tmp_path) -> None:
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content=json.dumps(
                    {"action": "modify", "fields_patch": {"人员": "房怡康"}},
                    ensure_ascii=False,
                ),
                tool_calls=[],
            )
        ]
    )
    loop = _build_loop(tmp_path, provider)
    create_tool = _FakeCreateTool()
    loop.tools.register(create_tool)
    loop.tools.register(_FakeFieldSchemaTool())

    session = loop.sessions.get_or_create("cli:chat")
    session.metadata["pending_write"] = {
        "tool": "bitable_create",
        "token": "tok-old",
        "args": {"table_id": "tbl_week", "fields": {"日期": "2026-03-08", "未完成事项": "A"}},
        "preview": {"action": "create", "table_id": "tbl_week", "fields": {"日期": "2026-03-08", "未完成事项": "A"}},
    }
    loop.sessions.save(session)

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="确认，人员是房怡康")
    )

    assert response is not None
    assert "确认" in response.content
    assert provider.calls == 1
    assert len(create_tool.calls) == 1
    assert create_tool.calls[0]["fields"]["人员"] == "房怡康"
    assert "confirm_token" not in create_tool.calls[0]

    refreshed = loop.sessions.get_or_create("cli:chat").metadata.get("pending_write") or {}
    assert refreshed["token"] == "tok-create-1"
    assert refreshed["args"]["fields"]["人员"] == "房怡康"
