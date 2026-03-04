import json
from pathlib import Path

import pytest

from nanobot.agent.skill_runtime.executor import SkillSpecExecutor
from nanobot.agent.skill_runtime.matcher import SkillSpecMatcher
from nanobot.agent.skill_runtime.output_guard import OutputGuard
from nanobot.agent.skill_runtime.param_parser import SkillSpecParamParser
from nanobot.agent.skill_runtime.registry import SkillSpecRegistry
from nanobot.agent.skill_runtime.user_memory import UserMemoryStore
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage
from nanobot.session.manager import Session


class _FakeTool(Tool):
    def __init__(self, name: str, result: dict, required: list[str] | None = None):
        self._name = name
        self.calls: list[dict] = []
        self._result = result
        self._required = required or []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "fake tool"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": self._required}

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        result = self._result
        if "confirm_token" in kwargs:
            result = {"success": True, "record_id": kwargs.get("record_id", "r1")}
        return json.dumps(result, ensure_ascii=False)


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def _build_registry(tmp_path: Path, spec_name: str, spec_yaml: str) -> SkillSpecRegistry:
    workspace = tmp_path / "workspace_specs"
    builtin = tmp_path / "builtin_specs"
    builtin.mkdir(parents=True, exist_ok=True)
    _write_yaml(workspace / f"{spec_name}.yaml", spec_yaml)
    registry = SkillSpecRegistry(workspace_root=workspace, builtin_root=builtin)
    registry.load()
    return registry


def test_matcher_prefers_explicit_then_regex_then_keywords(tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        "task_query",
        """
meta:
  id: task_query
  version: "0.1"
  description: 查任务
  match:
    regex: "任务详情\\s+\\w+"
    keywords: [任务, todo]
params: {type: object, properties: {query: {type: string}}}
action: {kind: query, table: {app_token: app, table_id: tbl}}
response: {}
error: {}
""",
    )
    matcher = SkillSpecMatcher(registry.specs)

    explicit = matcher.select("/skill task_query query=abc")
    assert explicit is not None
    assert explicit.reason == "explicit"

    by_regex = matcher.select("任务详情 T-123")
    assert by_regex is not None
    assert by_regex.reason == "regex"

    by_keywords = matcher.select("请帮我看下这个任务")
    assert by_keywords is not None
    assert by_keywords.reason == "keywords"


def test_param_parser_extracts_key_value_and_query() -> None:
    parser = SkillSpecParamParser()
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "status": {"type": "string", "default": "open"},
            "page_size": {"type": "integer", "default": 5},
        },
    }
    params = parser.parse("status=closed page_size=10 这是关键词", param_schema=schema)

    assert params["status"] == "closed"
    assert params["page_size"] == 10
    assert params["query"] == "这是关键词"


@pytest.mark.asyncio
async def test_executor_routes_query_with_permission_filter(tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        "task_query",
        """
meta: {id: task_query, version: "0.1", description: 搜任务}
params:
  type: object
  properties:
    query: {type: string}
action:
  kind: query
  table: {app_token: app_x, table_id: tbl_x}
  filter_template:
    op: and
    conditions:
      - field: title
        op: contains
        value: "{{ params.query }}"
response:
  output_policy:
    max_items: 5
error: {}
""",
    )
    tools = ToolRegistry()
    search = _FakeTool(
        "bitable_search",
        {
            "records": [
                {"fields": {"owner": "u-intern", "title": "A"}},
                {"fields": {"owner": "someone-else", "title": "B"}},
            ]
        },
    )
    tools.register(search)
    store = UserMemoryStore(tmp_path)
    store.write("feishu", "u-intern", {"role": "intern"})
    executor = SkillSpecExecutor(
        registry=registry,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=store,
    )
    session = Session("feishu:chat")

    result = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u-intern", chat_id="chat", content="/skill task_query alpha"),
        session,
    )

    assert result.handled is True
    assert "owner=u-intern" in result.content
    assert "someone-else" not in result.content
    assert search.calls[0]["app_token"] == "app_x"
    assert search.calls[0]["table_id"] == "tbl_x"
    assert search.calls[0]["keyword"] == "alpha"


@pytest.mark.asyncio
async def test_executor_cross_query_interpolates_previous_step(tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        "case_detail",
        """
meta: {id: case_detail, version: "0.1", description: 案件详情}
params:
  type: object
  properties:
    case_no: {type: string}
action:
  kind: query
  cross_query:
    steps:
      - id: case_base
        table: {app_token: app_case, table_id: tbl_case}
        filter_template:
          field: case_no
          op: contains
          value: "{{ params.case_no }}"
      - id: related_tasks
        table: {app_token: app_task, table_id: tbl_task}
        filter_template:
          field: case_id
          op: eq
          value: "{{ steps.case_base.rows[0].fields.case_id }}"
response: {}
error: {}
""",
    )

    class _CrossQueryTool(_FakeTool):
        async def execute(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("table_id") == "tbl_case":
                return json.dumps({"records": [{"fields": {"case_id": "C-001"}}]}, ensure_ascii=False)
            return json.dumps({"records": [{"fields": {"task": "T-1"}}]}, ensure_ascii=False)

    tools = ToolRegistry()
    search = _CrossQueryTool("bitable_search", {})
    tools.register(search)
    executor = SkillSpecExecutor(
        registry=registry,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
    )
    session = Session("feishu:chat")

    result = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content="/skill case_detail case_no=CASE-1"),
        session,
    )

    assert result.handled is True
    assert len(search.calls) == 2
    assert search.calls[0]["keyword"] == "CASE-1"
    assert search.calls[1]["filters"]["case_id"] == "C-001"


@pytest.mark.asyncio
async def test_executor_write_dry_run_and_confirm(tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        "task_update",
        """
meta: {id: task_update, version: "0.1", description: 更新任务}
params:
  type: object
  properties:
    record_id: {type: string}
    owner: {type: string}
action:
  kind: update
  table: {app_token: app_x, table_id: tbl_x}
  args:
    record_id: "{{ params.record_id }}"
    fields:
      owner: "{{ params.owner }}"
response: {}
error: {}
""",
    )
    tools = ToolRegistry()
    update_tool = _FakeTool("bitable_update", {"dry_run": True, "preview": {"ok": 1}, "confirm_token": "tok123"})
    tools.register(update_tool)
    store = UserMemoryStore(tmp_path)
    executor = SkillSpecExecutor(
        registry=registry,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=store,
    )
    session = Session("feishu:chat")

    dry_run = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content="/skill task_update record_id=r1 owner=u2"),
        session,
    )
    assert dry_run.handled is True
    assert "确认 tok123" in dry_run.content

    confirmed = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content="确认 tok123"),
        session,
    )
    assert confirmed.handled is True
    assert "success" in confirmed.content
    assert update_tool.calls[-1]["confirm_token"] == "tok123"


@pytest.mark.asyncio
async def test_executor_card_action_cancel_confirmation(tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        "task_update",
        """
meta: {id: task_update, version: "0.1", description: 更新任务}
params: {type: object, properties: {record_id: {type: string}}}
action: {kind: update, table: {app_token: app_x, table_id: tbl_x}, args: {record_id: "{{ params.record_id }}", fields: {status: open}}}
response: {}
error: {}
""",
    )
    tools = ToolRegistry()
    update_tool = _FakeTool("bitable_update", {"dry_run": True, "preview": {"ok": 1}, "confirm_token": "tok987"})
    tools.register(update_tool)
    executor = SkillSpecExecutor(
        registry=registry,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
    )
    session = Session("feishu:chat")

    await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content="/skill task_update record_id=r1"),
        session,
    )
    cancelled = await executor.execute_if_matched(
        InboundMessage(
            channel="feishu",
            sender_id="u1",
            chat_id="chat",
            content='[feishu card action trigger]\naction_value: {"confirm_token":"tok987"}',
            metadata={"msg_type": "card_action", "action_key": "cancel_write"},
        ),
        session,
    )

    assert cancelled.handled is True
    assert "已取消" in cancelled.content


@pytest.mark.asyncio
async def test_executor_document_pipeline_uses_media_paths(monkeypatch, tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        "doc_recognize",
        """
meta: {id: doc_recognize, version: "0.1", description: 文档识别}
params:
  type: object
  properties:
    paths:
      type: array
action:
  kind: document_pipeline
  args:
    paths: "{{ params.paths }}"
    skill_id: doc_recognize
response: {}
error: {}
""",
    )
    tools = ToolRegistry()
    executor = SkillSpecExecutor(
        registry=registry,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
    )
    session = Session("feishu:chat")

    captured: dict[str, object] = {}

    async def _fake_process_document(paths, skill_id, user_context):
        captured["paths"] = paths
        captured["skill_id"] = skill_id
        captured["user_context"] = user_context
        return {"results": [{"path": paths[0], "document_type": "contract"}], "errors": []}

    monkeypatch.setattr("nanobot.agent.skill_runtime.document_pipeline.process_document", _fake_process_document)

    result = await executor.execute_if_matched(
        InboundMessage(
            channel="feishu",
            sender_id="u1",
            chat_id="chat",
            content="/skill doc_recognize",
            media=["/tmp/sample-contract.pdf"],
        ),
        session,
    )

    assert result.handled is True
    assert captured["paths"] == ["/tmp/sample-contract.pdf"]
    assert captured["skill_id"] == "doc_recognize"
