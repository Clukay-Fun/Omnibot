import json
import re
from pathlib import Path
from typing import Any, cast

import pytest

from nanobot.agent.skill_runtime.executor import SkillSpecExecutor
from nanobot.agent.skill_runtime.matcher import SkillSpecMatcher
from nanobot.agent.skill_runtime.output_guard import OutputGuard
from nanobot.agent.skill_runtime.param_parser import SkillSpecParamParser
from nanobot.agent.skill_runtime.registry import SkillSpecRegistry
from nanobot.agent.skill_runtime.reminder_runtime import ReminderRuntime
from nanobot.agent.skill_runtime.user_memory import UserMemoryStore
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage
from nanobot.session.manager import Session


class _FixedEmbeddingRouter:
    def __init__(self, ranked: list[tuple[str, float]]):
        self._ranked = ranked

    def rank(self, query: str, specs: dict[str, Any]) -> list[tuple[str, float]]:  # noqa: ARG002
        return list(self._ranked)


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


class _CronFakeTool(Tool):
    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self._jobs: list[str] = []

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return "fake cron tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]}

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        action = kwargs.get("action")
        if action == "list":
            if not self._jobs:
                return "No scheduled jobs."
            return "Scheduled jobs:\n" + "\n".join(self._jobs)
        if action == "add":
            message = str(kwargs.get("message") or "")
            self._jobs.append(message)
            return f"Created job '{message}' (id: j1)"
        return "Error: unsupported action"


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


def test_matcher_rejects_low_score_embedding_match(tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        "task_query",
        """
meta: {id: task_query, version: "0.1", description: 查任务}
params: {type: object, properties: {query: {type: string}}}
action: {kind: query, table: {app_token: app, table_id: tbl}}
response: {}
error: {}
""",
    )
    matcher = SkillSpecMatcher(
        registry.specs,
        embedding_router=cast(Any, _FixedEmbeddingRouter([("task_query", 0.08)])),
        embedding_min_score=0.15,
    )

    selection = matcher.select("no lexical hit")

    assert selection is None


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
    assert result.metadata["skillspec_route"]["spec_id"] == "task_query"
    assert result.metadata["skillspec_route"]["reason"] == "explicit"
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


@pytest.mark.asyncio
async def test_executor_document_action_bridge_requires_confirm(tmp_path: Path, monkeypatch) -> None:
    registry = _build_registry(
        tmp_path,
        "doc_store",
        """
meta: {id: doc_store, version: "0.1", description: 文档入库}
params:
  type: object
  properties:
    paths:
      type: array
action:
  kind: document_pipeline
  args:
    paths: "{{ params.paths }}"
    skill_id: doc_store
  write_bridge:
    enabled: true
    confirm_required: true
    tool: bitable_create
    args:
      app_token: app_x
      table_id: tbl_x
      fields:
        title: "{{ result.results[0].document_type }}"
response: {}
error: {}
""",
    )
    tools = ToolRegistry()
    create_tool = _FakeTool("bitable_create", {"success": True, "record_id": "r-doc"})
    tools.register(create_tool)
    executor = SkillSpecExecutor(
        registry=registry,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
    )
    session = Session("feishu:chat")

    async def _fake_process_document(paths, skill_id, user_context):
        return {
            "results": [
                {
                    "path": paths[0],
                    "document_type": "invoice",
                    "extracted_fields": {"invoice_number": "INV-1", "total_amount": "$20.00"},
                    "write_ready": True,
                }
            ],
            "errors": [],
            "skill_id": skill_id,
            "user_context": user_context,
        }

    monkeypatch.setattr("nanobot.agent.skill_runtime.document_pipeline.process_document", _fake_process_document)

    first = await executor.execute_if_matched(
        InboundMessage(
            channel="feishu",
            sender_id="u1",
            chat_id="chat",
            content="/skill doc_store",
            media=["/tmp/sample-contract.pdf"],
        ),
        session,
    )

    assert first.handled is True
    assert "写入预览" in first.content
    match = re.search(r"确认\s+([a-z0-9]{10})", first.content)
    assert match is not None
    token = match.group(1)
    assert token in (session.metadata.get("skillspec_pending_writes") or {})

    confirm = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content=f"确认 {token}"),
        session,
    )
    assert confirm.handled is True
    assert "success" in confirm.content
    assert create_tool.calls[-1]["confirm_token"] == token
    assert create_tool.calls[-1]["fields"]["title"] == "invoice"
    assert session.metadata.get("skillspec_pending_writes") == {}


@pytest.mark.asyncio
async def test_executor_document_action_bridge_blocks_when_errors_without_writable_result(tmp_path: Path, monkeypatch) -> None:
    registry = _build_registry(
        tmp_path,
        "doc_store",
        """
meta: {id: doc_store, version: "0.1", description: 文档入库}
params: {type: object, properties: {paths: {type: array}}}
action:
  kind: document_pipeline
  args: {paths: "{{ params.paths }}", skill_id: doc_store}
  write_bridge:
    enabled: true
    confirm_required: true
    tool: bitable_create
    args:
      fields:
        extracted_fields: "{{ result.results[0].extracted_fields }}"
response: {}
error: {}
""",
    )
    tools = ToolRegistry()
    create_tool = _FakeTool("bitable_create", {"success": True})
    tools.register(create_tool)
    executor = SkillSpecExecutor(
        registry=registry,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
    )
    session = Session("feishu:chat")

    async def _fake_process_document(paths, skill_id, user_context):
        _ = (paths, skill_id, user_context)
        return {
            "results": [
                {
                    "path": "/tmp/a.pdf",
                    "document_type": "invoice",
                    "extracted_fields": {},
                    "write_ready": False,
                    "status": "template_missing",
                }
            ],
            "errors": ["[TEMPLATE_MISSING] /tmp/a.pdf: no template"],
        }

    monkeypatch.setattr("nanobot.agent.skill_runtime.document_pipeline.process_document", _fake_process_document)

    result = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content="/skill doc_store", media=["/tmp/a.pdf"]),
        session,
    )

    assert result.handled is True
    assert "无可写入结果" in result.content
    assert "确认" not in result.content
    assert len(create_tool.calls) == 0
    assert session.metadata.get("skillspec_pending_writes") in ({}, None)


@pytest.mark.asyncio
async def test_executor_document_action_bridge_supports_cancel(tmp_path: Path, monkeypatch) -> None:
    registry = _build_registry(
        tmp_path,
        "doc_store",
        """
meta: {id: doc_store, version: "0.1", description: 文档入库}
params: {type: object, properties: {paths: {type: array}}}
action:
  kind: document_pipeline
  args: {paths: "{{ params.paths }}", skill_id: doc_store}
  write_bridge: {enabled: true, confirm_required: true, tool: bitable_create}
response: {}
error: {}
""",
    )
    tools = ToolRegistry()
    create_tool = _FakeTool("bitable_create", {"success": True})
    tools.register(create_tool)
    executor = SkillSpecExecutor(
        registry=registry,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
    )
    session = Session("feishu:chat")

    async def _fake_process_document(paths, skill_id, user_context):
        _ = (paths, skill_id, user_context)
        return {"results": [{"document_type": "invoice"}], "errors": []}

    monkeypatch.setattr("nanobot.agent.skill_runtime.document_pipeline.process_document", _fake_process_document)

    first = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content="/skill doc_store", media=["/tmp/a.pdf"]),
        session,
    )
    token_match = re.search(r"取消\s+([a-z0-9]{10})", first.content)
    assert token_match is not None
    token = token_match.group(1)

    cancelled = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content=f"取消 {token}"),
        session,
    )

    assert cancelled.handled is True
    assert "已取消" in cancelled.content
    assert len(create_tool.calls) == 0
    assert session.metadata.get("skillspec_pending_writes") == {}


@pytest.mark.asyncio
async def test_executor_document_bridge_respects_auto_confirm_preference(tmp_path: Path, monkeypatch) -> None:
    registry = _build_registry(
        tmp_path,
        "doc_store",
        """
meta: {id: doc_store, version: "0.1", description: 文档入库}
params: {type: object, properties: {paths: {type: array}}}
action:
  kind: document_pipeline
  args: {paths: "{{ params.paths }}", skill_id: doc_store}
  write_bridge:
    enabled: true
    confirm_required: true
    confirm_respect_preference: true
    tool: bitable_create
    args:
      fields:
        document_type: "{{ result.results[0].document_type }}"
        channel: "{{ runtime.user_context.channel }}"
response: {}
error: {}
""",
    )
    tools = ToolRegistry()
    create_tool = _FakeTool("bitable_create", {"success": True, "record_id": "r-auto"})
    tools.register(create_tool)
    store = UserMemoryStore(tmp_path)
    store.write("feishu", "u-auto", {"skillspec": {"confirm_preference": "auto"}})
    executor = SkillSpecExecutor(
        registry=registry,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=store,
    )
    session = Session("feishu:chat")

    async def _fake_process_document(paths, skill_id, user_context):
        _ = (paths, skill_id, user_context)
        return {
            "results": [{"document_type": "invoice", "extracted_fields": {"invoice_number": "INV-1"}, "write_ready": True}],
            "errors": [],
        }

    monkeypatch.setattr("nanobot.agent.skill_runtime.document_pipeline.process_document", _fake_process_document)

    result = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u-auto", chat_id="chat", content="/skill doc_store", media=["/tmp/a.pdf"]),
        session,
    )

    assert result.handled is True
    assert "success" in result.content
    assert "确认" not in result.content
    assert len(create_tool.calls) == 1
    assert create_tool.calls[0]["fields"]["document_type"] == "invoice"
    assert create_tool.calls[0]["fields"]["channel"] == "feishu"


@pytest.mark.asyncio
async def test_executor_renders_query_with_field_mapping_and_template(tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        "mapped_query",
        """
meta: {id: mapped_query, version: "0.1", description: 映射模板查询}
params: {type: object, properties: {query: {type: string}}}
action:
  kind: query
  table: {app_token: app_x, table_id: tbl_x}
response:
  field_mapping:
    标题: title
    负责人: owner
  template: "{{ 标题 }} -> {{ 负责人 }}"
error: {}
""",
    )
    tools = ToolRegistry()
    tools.register(_FakeTool("bitable_search", {"records": [{"fields": {"title": "A", "owner": "u1"}}]}))
    executor = SkillSpecExecutor(
        registry=registry,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
    )
    session = Session("feishu:chat")

    result = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content="/skill mapped_query alpha"),
        session,
    )

    assert result.handled is True
    assert "A -> u1" in result.content


@pytest.mark.asyncio
async def test_executor_renders_jinja_template_with_if_for_blocks(tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        "reminder_list",
        """
meta: {id: reminder_list, version: "0.1", description: 提醒列表}
params: {type: object, properties: {}}
action: {kind: reminder_list}
response:
  template: |
    {% if reminders %}
    当前提醒 {{ reminders | length }} 条：
    {% for item in reminders %}
    - {{ item.id }} | {{ item.text }}
    {% endfor %}
    {% else %}
    暂无提醒。
    {% endif %}
error: {}
""",
    )
    tools = ToolRegistry()
    executor = SkillSpecExecutor(
        registry=registry,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
        reminder_runtime=ReminderRuntime(tmp_path / "reminders.json"),
    )
    session = Session("feishu:chat")
    store = ReminderRuntime(tmp_path / "reminders.json")
    await store.create_reminder(
        user_id="u1",
        chat_id="chat",
        text="PayBill",
        due_at="2026-03-06T10:00:00",
        channel="feishu",
    )

    result = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content="/skill reminder_list"),
        session,
    )

    assert result.handled is True
    assert "当前提醒 1 条" in result.content
    assert "PayBill" in result.content


@pytest.mark.asyncio
async def test_executor_reminder_guard_blocks_non_reminder_text_but_allows_explicit(tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        "reminder_list",
        """
meta: {id: reminder_list, version: "0.1", description: 提醒列表}
params: {type: object, properties: {}}
action: {kind: reminder_list}
response: {}
error: {}
""",
    )
    executor = SkillSpecExecutor(
        registry=registry,
        tools=ToolRegistry(),
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
        embedding_router=cast(Any, _FixedEmbeddingRouter([("reminder_list", 0.91)])),
        reminder_runtime=ReminderRuntime(tmp_path / "reminders.json"),
    )
    session = Session("feishu:chat")

    blocked = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content="叫我什么"),
        session,
    )
    assert blocked.handled is False

    explicit = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content="/skill reminder_list"),
        session,
    )
    assert explicit.handled is True


@pytest.mark.asyncio
async def test_executor_smalltalk_guard_blocks_embedding_false_positive(tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        "task_query",
        """
meta: {id: task_query, version: "0.1", description: 查询任务数据}
params: {type: object, properties: {query: {type: string}}}
action:
  kind: query
  table: {app_token: app_x, table_id: tbl_x}
response: {}
error: {}
""",
    )
    tools = ToolRegistry()
    search = _FakeTool("bitable_search", {"records": []})
    tools.register(search)
    executor = SkillSpecExecutor(
        registry=registry,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
        embedding_router=cast(Any, _FixedEmbeddingRouter([("task_query", 0.95)])),
    )
    session = Session("feishu:chat")

    blocked = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content="您能干嘛"),
        session,
    )
    assert blocked.handled is False
    assert search.calls == []

    routed = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content="查任务"),
        session,
    )
    assert routed.handled is True
    assert len(search.calls) == 1


@pytest.mark.asyncio
async def test_executor_field_mapping_takes_precedence_in_template_context(tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        "mapped_query_priority",
        """
meta: {id: mapped_query_priority, version: "0.1", description: 映射优先级}
params: {type: object, properties: {query: {type: string}}}
action:
  kind: query
  table: {app_token: app_x, table_id: tbl_x}
response:
  field_mapping:
    标题: title
  template: "{{ 标题 }}"
error: {}
""",
    )
    tools = ToolRegistry()
    tools.register(
        _FakeTool(
            "bitable_search",
            {
                "records": [
                    {
                        "fields": {"title": "MappedTitle", "标题": "RawTitle"},
                    }
                ]
            },
        )
    )
    executor = SkillSpecExecutor(
        registry=registry,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
    )
    session = Session("feishu:chat")

    result = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content="/skill mapped_query_priority alpha"),
        session,
    )

    assert result.handled is True
    assert "MappedTitle" in result.content
    assert "RawTitle" not in result.content


@pytest.mark.asyncio
async def test_executor_marks_sensitive_group_result_for_private_delivery(tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        "sensitive_query",
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
    tools = ToolRegistry()
    tools.register(_FakeTool("bitable_search", {"records": [{"fields": {"title": "A"}}]}))
    executor = SkillSpecExecutor(
        registry=registry,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
    )
    session = Session("feishu:chat")

    result = await executor.execute_if_matched(
        InboundMessage(
            channel="feishu",
            sender_id="u-sensitive",
            chat_id="oc_group",
            content="/skill sensitive_query alpha",
            metadata={"chat_type": "group"},
        ),
        session,
    )

    assert result.handled is True
    assert result.reply_chat_id == "u-sensitive"
    assert result.metadata["private_delivery"] is True


@pytest.mark.asyncio
async def test_executor_confirm_respects_user_preference_for_auto_confirm(tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        "task_update_pref",
        """
meta: {id: task_update_pref, version: "0.1", description: 更新任务}
params:
  type: object
  properties:
    record_id: {type: string}
action:
  kind: update
  table: {app_token: app_x, table_id: tbl_x}
  args:
    record_id: "{{ params.record_id }}"
    fields:
      status: done
response:
  confirm_required: true
  confirm_respect_preference: true
error: {}
""",
    )
    tools = ToolRegistry()
    update_tool = _FakeTool("bitable_update", {"dry_run": True, "preview": {"ok": 1}, "confirm_token": "tok456"})
    tools.register(update_tool)
    store = UserMemoryStore(tmp_path)
    store.write("feishu", "u-auto", {"skillspec": {"confirm_preference": "auto"}})
    executor = SkillSpecExecutor(
        registry=registry,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=store,
    )
    session = Session("feishu:chat")

    result = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u-auto", chat_id="chat", content="/skill task_update_pref record_id=r1"),
        session,
    )

    assert result.handled is True
    assert "success" in result.content
    assert len(update_tool.calls) == 2
    assert update_tool.calls[1]["confirm_token"] == "tok456"


@pytest.mark.asyncio
async def test_executor_reminder_set_list_cancel_flow(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace_specs"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_yaml(
        workspace / "reminder_set.yaml",
        """
meta: {id: reminder_set, version: "0.1", description: 设置提醒}
params: {type: object, properties: {text: {type: string}, due_at: {type: string}}}
action: {kind: reminder_set}
response: {}
error: {}
""",
    )
    _write_yaml(
        workspace / "reminder_list.yaml",
        """
meta: {id: reminder_list, version: "0.1", description: 列出提醒}
params: {type: object, properties: {}}
action: {kind: reminder_list}
response: {}
error: {}
""",
    )
    _write_yaml(
        workspace / "reminder_cancel.yaml",
        """
meta: {id: reminder_cancel, version: "0.1", description: 取消提醒}
params: {type: object, properties: {reminder_id: {type: string}}}
action: {kind: reminder_cancel}
response: {}
error: {}
""",
    )
    registry = SkillSpecRegistry(workspace_root=workspace, builtin_root=tmp_path / "builtin_specs")
    registry.load()

    executor = SkillSpecExecutor(
        registry=registry,
        tools=ToolRegistry(),
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
        reminder_runtime=ReminderRuntime(tmp_path / "reminders.json"),
    )
    session = Session("feishu:chat")

    created = await executor.execute_if_matched(
        InboundMessage(
            channel="feishu",
            sender_id="u1",
            chat_id="chat",
            content="/skill reminder_set text=Pay_bill due_at=2026-03-06T10:00:00",
        ),
        session,
    )
    assert created.handled is True
    assert "r000001" in created.content

    listed = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content="/skill reminder_list"),
        session,
    )
    assert listed.handled is True
    assert "Pay_bill" in listed.content

    cancelled = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content="/skill reminder_cancel reminder_id=r000001"),
        session,
    )
    assert cancelled.handled is True
    assert '"cancelled": true' in cancelled.content

    listed_after_cancel = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content="/skill reminder_list"),
        session,
    )
    assert "\"reminders\": []" in listed_after_cancel.content


@pytest.mark.asyncio
async def test_executor_daily_summary_generation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace_specs"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_yaml(
        workspace / "reminder_set.yaml",
        """
meta: {id: reminder_set, version: "0.1", description: 设置提醒}
params: {type: object, properties: {text: {type: string}, due_at: {type: string}}}
action: {kind: reminder_set}
response: {}
error: {}
""",
    )
    _write_yaml(
        workspace / "daily_summary.yaml",
        """
meta: {id: daily_summary, version: "0.1", description: 每日汇总}
params: {type: object, properties: {date: {type: string}}}
action: {kind: daily_summary}
response: {}
error: {}
""",
    )
    registry = SkillSpecRegistry(workspace_root=workspace, builtin_root=tmp_path / "builtin_specs")
    registry.load()
    executor = SkillSpecExecutor(
        registry=registry,
        tools=ToolRegistry(),
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
        reminder_runtime=ReminderRuntime(tmp_path / "reminders.json"),
    )
    session = Session("feishu:chat")

    await executor.execute_if_matched(
        InboundMessage(
            channel="feishu",
            sender_id="u1",
            chat_id="chat",
            content="/skill reminder_set text=TaskA due_at=2026-03-06T10:00:00",
        ),
        session,
    )
    await executor.execute_if_matched(
        InboundMessage(
            channel="feishu",
            sender_id="u1",
            chat_id="chat",
            content="/skill reminder_set text=TaskB due_at=2026-03-07T10:00:00",
        ),
        session,
    )

    summary = await executor.execute_if_matched(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat", content="/skill daily_summary date=2026-03-06"),
        session,
    )
    assert summary.handled is True
    assert '"due_today_count": 1' in summary.content
    assert "TaskA" in summary.content


@pytest.mark.asyncio
async def test_executor_reminder_set_graceful_when_calendar_unavailable(tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        "reminder_set",
        """
meta: {id: reminder_set, version: "0.1", description: 设置提醒}
params: {type: object, properties: {text: {type: string}, due_at: {type: string}, calendar_sync: {type: boolean}}}
action: {kind: reminder_set, calendar_enabled: true}
response: {}
error: {}
""",
    )
    executor = SkillSpecExecutor(
        registry=registry,
        tools=ToolRegistry(),
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
        reminder_runtime=ReminderRuntime(tmp_path / "reminders.json"),
    )
    session = Session("feishu:chat")

    created = await executor.execute_if_matched(
        InboundMessage(
            channel="feishu",
            sender_id="u1",
            chat_id="chat",
            content="/skill reminder_set text=SyncTest due_at=2026-03-06T10:00:00 calendar_sync=true",
        ),
        session,
    )

    assert created.handled is True
    assert '"status": "unavailable"' in created.content
    assert "SyncTest" in created.content


@pytest.mark.asyncio
async def test_executor_reminder_record_bridge_success_and_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace_specs"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_yaml(
        workspace / "reminder_set.yaml",
        """
meta: {id: reminder_set, version: "0.1", description: 设置提醒}
params: {type: object, properties: {text: {type: string}, due_at: {type: string}}}
action:
  kind: reminder_set
  record_bridge:
    enabled: true
    tool: bitable_create
    args:
      fields:
        reminder_id: "{{ runtime.reminder.id }}"
        text: "{{ runtime.reminder.text }}"
response: {}
error: {}
""",
    )
    registry = SkillSpecRegistry(workspace_root=workspace, builtin_root=tmp_path / "builtin_specs")
    registry.load()

    tools = ToolRegistry()
    create_tool = _FakeTool("bitable_create", {"dry_run": True, "preview": {"ok": 1}, "confirm_token": "b1"})
    tools.register(create_tool)
    executor = SkillSpecExecutor(
        registry=registry,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
        reminder_runtime=ReminderRuntime(tmp_path / "reminders.json"),
    )
    session = Session("feishu:chat")

    created = await executor.execute_if_matched(
        InboundMessage(
            channel="feishu",
            sender_id="u1",
            chat_id="chat",
            content="/skill reminder_set text=BridgeOk due_at=2026-03-06T10:00:00",
        ),
        session,
    )
    assert created.handled is True
    created_payload = json.loads(created.content)
    assert created_payload["bridges"]["record_bridge"]["status"] == "created"
    assert len(create_tool.calls) == 2
    assert create_tool.calls[1]["confirm_token"] == "b1"

    tools_fail = ToolRegistry()
    failing_tool = _FakeTool("bitable_create", {"error": "table unavailable"})
    tools_fail.register(failing_tool)
    executor_fail = SkillSpecExecutor(
        registry=registry,
        tools=tools_fail,
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
        reminder_runtime=ReminderRuntime(tmp_path / "reminders-fail.json"),
    )
    failed = await executor_fail.execute_if_matched(
        InboundMessage(
            channel="feishu",
            sender_id="u1",
            chat_id="chat",
            content="/skill reminder_set text=BridgeFail due_at=2026-03-07T10:00:00",
        ),
        Session("feishu:chat"),
    )
    assert failed.handled is True
    failed_payload = json.loads(failed.content)
    assert failed_payload["reminder"]["text"] == "BridgeFail"
    assert failed_payload["bridges"]["record_bridge"]["status"] == "failed"


@pytest.mark.asyncio
async def test_executor_reminder_calendar_bridge_unconfigured_and_failure(tmp_path: Path) -> None:
    registry_unconfigured = _build_registry(
        tmp_path,
        "reminder_set",
        """
meta: {id: reminder_set, version: "0.1", description: 设置提醒}
params: {type: object, properties: {text: {type: string}, due_at: {type: string}, calendar_sync: {type: boolean}}}
action: {kind: reminder_set, calendar_enabled: true}
response: {}
error: {}
""",
    )
    executor_unconfigured = SkillSpecExecutor(
        registry=registry_unconfigured,
        tools=ToolRegistry(),
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
        reminder_runtime=ReminderRuntime(tmp_path / "reminders-a.json"),
    )
    result_unconfigured = await executor_unconfigured.execute_if_matched(
        InboundMessage(
            channel="feishu",
            sender_id="u1",
            chat_id="chat",
            content="/skill reminder_set text=NoCfg due_at=2026-03-06T10:00:00 calendar_sync=true",
        ),
        Session("feishu:chat"),
    )
    assert result_unconfigured.handled is True
    unconfigured_payload = json.loads(result_unconfigured.content)
    assert unconfigured_payload["bridges"]["calendar_bridge"]["status"] == "skipped"
    assert unconfigured_payload["bridges"]["calendar_bridge"]["reason"] == "not_configured"

    registry_fail = _build_registry(
        tmp_path,
        "reminder_set_fail",
        """
meta: {id: reminder_set_fail, version: "0.1", description: 设置提醒}
params: {type: object, properties: {text: {type: string}, due_at: {type: string}, calendar_sync: {type: boolean}}}
action:
  kind: reminder_set
  calendar_bridge:
    enabled: true
    tool: calendar_create
    args:
      title: "{{ runtime.reminder.text }}"
response: {}
error: {}
""",
    )
    tools = ToolRegistry()
    tools.register(_FakeTool("calendar_create", {"error": "calendar api down"}))
    executor_fail = SkillSpecExecutor(
        registry=registry_fail,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
        reminder_runtime=ReminderRuntime(tmp_path / "reminders-b.json"),
    )
    result_fail = await executor_fail.execute_if_matched(
        InboundMessage(
            channel="feishu",
            sender_id="u1",
            chat_id="chat",
            content="/skill reminder_set_fail text=CalFail due_at=2026-03-06T10:00:00 calendar_sync=true",
        ),
        Session("feishu:chat"),
    )
    assert result_fail.handled is True
    fail_payload = json.loads(result_fail.content)
    assert fail_payload["bridges"]["calendar_bridge"]["status"] == "failed"


@pytest.mark.asyncio
async def test_executor_reminder_summary_cron_bridge_add_path(tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        "reminder_set",
        """
meta: {id: reminder_set, version: "0.1", description: 设置提醒}
params: {type: object, properties: {text: {type: string}, due_at: {type: string}}}
action:
  kind: reminder_set
  summary_cron_bridge:
    enabled: true
    tool: cron
    dedupe_key_template: "daily_summary:{{ runtime.user_context.sender_id }}"
    args:
      action: add
      cron_expr: "0 9 * * *"
      message: "daily_summary:{{ runtime.user_context.sender_id }}"
response: {}
error: {}
""",
    )
    tools = ToolRegistry()
    cron_tool = _CronFakeTool()
    tools.register(cron_tool)
    executor = SkillSpecExecutor(
        registry=registry,
        tools=tools,
        output_guard=OutputGuard(),
        user_memory=UserMemoryStore(tmp_path),
        reminder_runtime=ReminderRuntime(tmp_path / "reminders-cron.json"),
    )

    first = await executor.execute_if_matched(
        InboundMessage(
            channel="feishu",
            sender_id="u-cron",
            chat_id="chat",
            content="/skill reminder_set text=CronA due_at=2026-03-06T10:00:00",
        ),
        Session("feishu:chat"),
    )
    assert first.handled is True
    first_payload = json.loads(first.content)
    assert first_payload["bridges"]["summary_cron_bridge"]["status"] == "created"

    second = await executor.execute_if_matched(
        InboundMessage(
            channel="feishu",
            sender_id="u-cron",
            chat_id="chat",
            content="/skill reminder_set text=CronB due_at=2026-03-07T10:00:00",
        ),
        Session("feishu:chat"),
    )
    assert second.handled is True
    second_payload = json.loads(second.content)
    assert second_payload["bridges"]["summary_cron_bridge"]["status"] == "skipped"
    assert second_payload["bridges"]["summary_cron_bridge"]["reason"] == "duplicate"

    add_calls = [call for call in cron_tool.calls if call.get("action") == "add"]
    assert len(add_calls) == 1
