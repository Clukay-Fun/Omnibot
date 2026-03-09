import json
from typing import Any

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import Tool
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import SkillSpecConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest


class _SilentProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, **kwargs: Any) -> LLMResponse:
        _ = kwargs
        self.calls += 1
        return LLMResponse(content="llm-fallback", tool_calls=[])

    def get_default_model(self) -> str:
        return "dummy"


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
            {"open_id": f"ou_{idx}", "display_name": f"联系人{idx}", "matched": {"邮箱": f"user{idx}@example.com"}}
            for idx in range(1, 8)
        ]
        keyword = str(kwargs.get("keyword") or "").strip()
        if keyword:
            contacts = [item for item in contacts if keyword in item["display_name"]]
        return json.dumps({"keyword": keyword, "contacts": contacts, "total": len(contacts)}, ensure_ascii=False)


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
        return LLMResponse(content="unexpected", tool_calls=[])

    def get_default_model(self) -> str:
        return "dummy"


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


class _FakePrepareCreateAmbiguousRecordTool(Tool):
    @property
    def name(self) -> str:
        return "bitable_prepare_create"

    @property
    def description(self) -> str:
        return "fake prepare create with ambiguous records"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs
        return json.dumps(
            {
                "request_text": "更新合同，合同编号 HT-001，合同状态 已签署",
                "needs_table_confirmation": False,
                "selected_table": {"table_id": "tbl_contract", "name": "合同管理"},
                "draft_fields": {"合同编号": "HT-001", "合同状态": "已签署"},
                "identity_strategy": ["合同编号"],
                "record_lookup": {
                    "attempted": True,
                    "matched": 2,
                    "records": [
                        {"record_id": "rec_contract_1", "fields": {"合同编号": "HT-001"}},
                        {"record_id": "rec_contract_2", "fields": {"合同编号": "HT-001-旧"}},
                    ],
                },
                "operation_guess": "ambiguous_existing",
                "needs_record_confirmation": True,
                "next_step": None,
            },
            ensure_ascii=False,
        )


class _FakePrepareCreateCaseUpdateTool(Tool):
    @property
    def name(self) -> str:
        return "bitable_prepare_create"

    @property
    def description(self) -> str:
        return "fake prepare create with matched case record"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs
        return json.dumps(
            {
                "request_text": "更新案件，案号 (2026)京01民初123号，案件状态 已立案",
                "needs_table_confirmation": False,
                "selected_table": {"table_id": "tbl_case", "name": "案件项目总库"},
                "profile": {"display_name": "案件项目总库", "aliases": ["案件项目总库", "案件库"]},
                "draft_fields": {"案号": "(2026)京01民初123号", "案件状态": "已立案"},
                "identity_strategy": ["案号"],
                "record_lookup": {
                    "attempted": True,
                    "matched": 1,
                    "records": [{"record_id": "rec_case_1", "fields": {"案号": "(2026)京01民初123号"}}],
                },
                "operation_guess": "update_existing",
                "needs_record_confirmation": False,
                "next_step": {
                    "tool": "bitable_update",
                    "mode": "dry_run",
                    "arguments": {"table_id": "tbl_case", "record_id": "rec_case_1", "fields": {"案件状态": "已立案"}},
                },
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
        return json.dumps(
            {
                "dry_run": True,
                "preview": {
                    "action": "update",
                    "table_id": kwargs.get("table_id", "tbl_contract"),
                    "record_id": kwargs.get("record_id"),
                    "fields": kwargs.get("fields", {}),
                },
                "confirm_token": "tok-update-1",
            },
            ensure_ascii=False,
        )


def _build_loop(tmp_path) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=_SilentProvider(),
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=False),
    )


@pytest.mark.asyncio
async def test_contact_query_coordinator_stores_recent_directory_hits_and_selection_state(tmp_path) -> None:
    loop = _build_loop(tmp_path)
    tool = _FakeDirectorySearchTool()
    loop.tools.register(tool)

    response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="通讯录里都有谁")
    )

    assert response is not None
    assert "联系人1" in response.content
    session = loop.sessions.get_or_create("feishu:ou_chat")
    assert len(session.metadata["recent_directory_hits"]) == 7
    assert session.metadata["result_selection"]["kind"] == "directory_contacts"
    assert session.metadata["result_selection"]["offset"] == 5


@pytest.mark.asyncio
async def test_contact_query_coordinator_logs_short_circuit(tmp_path, monkeypatch) -> None:
    loop = _build_loop(tmp_path)
    loop.tools.register(_FakeDirectorySearchTool())
    events: list[tuple[str, str, str]] = []

    def _capture(name: str, session_key: str, source: str) -> None:
        events.append((name, session_key, source))

    monkeypatch.setattr(loop, "_log_coordinator_hit", _capture)

    await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="通讯录里都有谁")
    )

    assert events == [("ContactQueryCoordinator", "feishu:ou_chat", "message")]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["查一下今天日程", "搜一下云文档有哪些文档"])
async def test_contact_query_coordinator_skips_non_directory_lookup_phrasing(tmp_path, content) -> None:
    loop = _build_loop(tmp_path)
    tool = _FakeDirectorySearchTool()
    loop.tools.register(tool)

    response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content=content)
    )

    assert response is not None
    assert response.content == "llm-fallback"
    assert tool.calls == []


@pytest.mark.asyncio
async def test_continuation_coordinator_pages_recent_directory_hits(tmp_path) -> None:
    loop = _build_loop(tmp_path)
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


@pytest.mark.asyncio
async def test_continuation_coordinator_keeps_global_selection_numbers_on_later_pages(tmp_path) -> None:
    loop = _build_loop(tmp_path)
    session = loop.sessions.get_or_create("feishu:ou_chat")
    session.metadata["result_selection"] = {
        "kind": "table_candidates",
        "items": [
            {"table_id": f"tbl_{idx}", "name": f"候选表{idx}"}
            for idx in range(1, 8)
        ],
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


@pytest.mark.asyncio
async def test_result_selection_coordinator_picks_second_table_candidate(tmp_path) -> None:
    loop = _build_loop(tmp_path)
    session = loop.sessions.get_or_create("feishu:ou_chat")
    session.metadata["result_selection"] = {
        "kind": "table_candidates",
        "items": [
            {"table_id": "tbl_case", "name": "案件项目总库"},
            {"table_id": "tbl_week", "name": "团队周工作计划表"},
        ],
        "offset": 2,
        "page_size": 5,
    }
    loop.sessions.save(session)

    response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="第二个")
    )

    assert response is not None
    assert "团队周工作计划表" in response.content
    assert loop.sessions.get_or_create("feishu:ou_chat").metadata["recent_selected_table"]["table_id"] == "tbl_week"


@pytest.mark.asyncio
async def test_reference_resolution_coordinator_answers_recent_table_and_message_questions(tmp_path) -> None:
    loop = _build_loop(tmp_path)
    session = loop.sessions.get_or_create("feishu:ou_chat")
    session.metadata["recent_selected_table"] = {"table_id": "tbl_week", "table_name": "团队周工作计划表"}
    session.metadata["referenced_message"] = {"message_id": "om_prev", "summary": "机器人原回复：天气晴朗"}
    loop.sessions.save(session)

    table_response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="刚才那个表是什么")
    )
    ref_response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="那条消息说了什么")
    )

    assert table_response is not None
    assert "团队周工作计划表" in table_response.content
    assert ref_response is not None
    assert "天气晴朗" in ref_response.content


@pytest.mark.asyncio
async def test_result_selection_coordinator_short_circuits_ambiguous_prepare_create_result(tmp_path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_ToolCallingProvider(),
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=False),
    )
    loop.tools.register(_FakePrepareCreateTool())

    response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="帮我新增工作记录")
    )

    assert response is not None
    assert "候选表" in response.content
    assert "团队周工作计划表" in response.content
    session = loop.sessions.get_or_create("feishu:ou_chat")
    assert session.metadata["result_selection"]["kind"] == "table_candidates"


@pytest.mark.asyncio
async def test_result_selection_coordinator_logs_tool_result_short_circuit(tmp_path, monkeypatch) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_ToolCallingProvider(),
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=False),
    )
    loop.tools.register(_FakePrepareCreateTool())
    events: list[tuple[str, str, str]] = []

    def _capture(name: str, session_key: str, source: str) -> None:
        events.append((name, session_key, source))

    monkeypatch.setattr(loop, "_log_coordinator_hit", _capture)

    await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="帮我新增工作记录")
    )

    assert ("ResultSelectionCoordinator", "feishu:ou_chat", "tool_result") in events


@pytest.mark.asyncio
async def test_result_selection_coordinator_prompts_for_ambiguous_record_candidates(tmp_path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_ToolCallingProvider(),
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=False),
    )
    loop.tools.register(_FakePrepareCreateAmbiguousRecordTool())

    response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="更新合同，合同编号 HT-001，合同状态 已签署")
    )

    assert response is not None
    assert "候选记录" in response.content
    assert "rec_contract_1" in response.content
    session = loop.sessions.get_or_create("feishu:ou_chat")
    assert session.metadata["result_selection"]["kind"] == "record_candidates"
    assert session.metadata["record_selection_action"]["tool"] == "bitable_update"


@pytest.mark.asyncio
async def test_result_selection_coordinator_executes_selected_record_update_preview(tmp_path) -> None:
    loop = _build_loop(tmp_path)
    update_tool = _FakeUpdateTool()
    loop.tools.register(update_tool)
    session = loop.sessions.get_or_create("feishu:ou_chat")
    session.metadata["result_selection"] = {
        "kind": "record_candidates",
        "items": [
            {"record_id": "rec_contract_1", "fields": {"合同编号": "HT-001"}},
            {"record_id": "rec_contract_2", "fields": {"合同编号": "HT-001-旧"}},
        ],
        "offset": 2,
        "page_size": 5,
    }
    session.metadata["record_selection_action"] = {
        "tool": "bitable_update",
        "table_id": "tbl_contract",
        "draft_fields": {"合同编号": "HT-001", "合同状态": "已签署"},
        "identity_strategy": ["合同编号"],
        "request_text": "更新合同，合同编号 HT-001，合同状态 已签署",
    }
    loop.sessions.save(session)

    response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="第一个")
    )

    assert response is not None
    assert "确认" in response.content
    assert update_tool.calls == [
        {"table_id": "tbl_contract", "record_id": "rec_contract_1", "fields": {"合同状态": "已签署"}}
    ]
    refreshed = loop.sessions.get_or_create("feishu:ou_chat")
    pending = refreshed.metadata.get("pending_write") or {}
    assert pending["tool"] == "bitable_update"
    assert pending["args"]["record_id"] == "rec_contract_1"


@pytest.mark.asyncio
async def test_prepare_create_stores_recent_case_object_history(tmp_path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_ToolCallingProvider(),
        workspace=tmp_path,
        skillspec_config=SkillSpecConfig(enabled=False),
    )
    loop.tools.register(_FakePrepareCreateCaseUpdateTool())
    loop.tools.register(_FakeUpdateTool())

    await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="更新案件，案号 (2026)京01民初123号，案件状态 已立案")
    )

    session = loop.sessions.get_or_create("feishu:ou_chat")
    history = session.metadata.get("recent_case_objects") or []
    assert history[0]["record_id"] == "rec_case_1"
    assert history[0]["identity_values"]["案号"] == "(2026)京01民初123号"


@pytest.mark.asyncio
async def test_reference_resolution_coordinator_reads_recent_contract_history(tmp_path) -> None:
    loop = _build_loop(tmp_path)
    session = loop.sessions.get_or_create("feishu:ou_chat")
    session.metadata["recent_contract_objects"] = [
        {"display_label": "HT-001 / 星火科技", "record_id": "rec_contract_1"},
        {"display_label": "HT-000 / 老合同", "record_id": "rec_contract_0"},
    ]
    loop.sessions.save(session)

    response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="刚才那个合同是什么")
    )

    assert response is not None
    assert "HT-001 / 星火科技" in response.content

    response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="ou_user", chat_id="ou_chat", content="上一个合同是什么")
    )

    assert response is not None
    assert "HT-000 / 老合同" in response.content
