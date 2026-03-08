"""描述:
主要功能:
    - 验证上下文构建中的提示词缓存稳定性。
"""

from __future__ import annotations

from datetime import datetime as real_datetime
import json
from pathlib import Path
import datetime as datetime_module

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanobot.agent.prompt_context import PromptContext


#region 测试辅助


class _FakeDatetime(real_datetime):
    """用处，参数

    功能:
        - 提供可控时间用于测试提示词稳定性。
    """
    current = real_datetime(2026, 2, 24, 13, 59)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        """用处，参数

        功能:
            - 返回当前预设的测试时间。
        """
        return cls.current


def _make_workspace(tmp_path: Path) -> Path:
    """用处，参数

    功能:
        - 创建并返回隔离的测试工作目录。
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    return workspace


#endregion

#region 提示词缓存测试


def test_system_prompt_stays_stable_when_clock_changes(tmp_path, monkeypatch) -> None:
    """用处，参数

    功能:
        - 验证仅时间变化不会影响系统提示词内容。
    """
    monkeypatch.setattr(datetime_module, "datetime", _FakeDatetime)

    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    _FakeDatetime.current = real_datetime(2026, 2, 24, 13, 59)
    prompt1 = builder.build_system_prompt()

    _FakeDatetime.current = real_datetime(2026, 2, 24, 14, 0)
    prompt2 = builder.build_system_prompt()

    assert prompt1 == prompt2


def test_runtime_context_is_separate_untrusted_user_message(tmp_path) -> None:
    """用处，参数

    功能:
        - 验证运行时元数据作为独立用户消息注入。
    """
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[],
        current_message="Return exactly: OK",
        channel="cli",
        chat_id="direct",
    )

    assert messages[0]["role"] == "system"
    assert "## Current Session" not in messages[0]["content"]

    assert messages[-2]["role"] == "user"
    runtime_content = messages[-2]["content"]
    assert isinstance(runtime_content, str)
    assert ContextBuilder._RUNTIME_CONTEXT_TAG in runtime_content
    assert "Current Time:" in runtime_content
    assert "Channel: cli" in runtime_content
    assert "Chat ID: direct" in runtime_content

    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "Return exactly: OK"


def test_feishu_memory_scope_isolated_by_runtime(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    (workspace / "MEMORY.md").write_text("global-memory", encoding="utf-8")

    memory = MemoryStore(workspace)
    user_memory = memory.feishu_user_memory_path("ou_user")
    user_memory.parent.mkdir(parents=True, exist_ok=True)
    user_memory.write_text("private-user-memory", encoding="utf-8")

    chat_memory = memory.feishu_chat_memory_path("oc_group")
    chat_memory.parent.mkdir(parents=True, exist_ok=True)
    chat_memory.write_text("group-memory", encoding="utf-8")

    thread_memory = memory.feishu_thread_memory_path("oc_group", "omt_topic")
    thread_memory.parent.mkdir(parents=True, exist_ok=True)
    thread_memory.write_text("thread-memory", encoding="utf-8")

    builder = ContextBuilder(workspace)
    private_prompt = builder.build_system_prompt(
        runtime=PromptContext(channel="feishu", chat_id="ou_user", sender_id="ou_user", metadata={"chat_type": "p2p"})
    )
    group_prompt = builder.build_system_prompt(
        runtime=PromptContext(channel="feishu", chat_id="oc_group", sender_id="ou_user", metadata={"chat_type": "group"})
    )
    topic_prompt = builder.build_system_prompt(
        runtime=PromptContext(
            channel="feishu",
            chat_id="oc_group",
            sender_id="ou_user",
            metadata={"chat_type": "group", "thread_id": "omt_topic"},
        )
    )

    assert "global-memory" not in private_prompt
    assert "private-user-memory" in private_prompt

    assert "global-memory" in group_prompt
    assert "group-memory" in group_prompt
    assert "private-user-memory" not in group_prompt

    assert "group-memory" in topic_prompt
    assert "thread-memory" in topic_prompt
    assert "global-memory" not in topic_prompt


def test_private_feishu_long_term_memory_reads_and_writes_user_scope(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    (workspace / "MEMORY.md").write_text("shared-memory", encoding="utf-8")

    memory = MemoryStore(workspace)
    user_memory = memory.feishu_user_memory_path("ou_user")
    user_memory.parent.mkdir(parents=True, exist_ok=True)
    user_memory.write_text("private-memory", encoding="utf-8")

    private_runtime = PromptContext(
        channel="feishu",
        chat_id="ou_user",
        sender_id="ou_user",
        metadata={"chat_type": "p2p"},
    )
    group_runtime = PromptContext(
        channel="feishu",
        chat_id="oc_group",
        sender_id="ou_user",
        metadata={"chat_type": "group"},
    )

    assert memory.read_long_term(runtime=private_runtime) == "private-memory"
    assert memory.read_long_term(runtime=group_runtime) == "shared-memory"

    memory.write_long_term("private-memory-updated", runtime=private_runtime)

    assert user_memory.read_text(encoding="utf-8") == "private-memory-updated"
    assert (workspace / "MEMORY.md").read_text(encoding="utf-8") == "shared-memory"


def test_add_tool_result_compacts_bitable_table_metadata_for_llm(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)
    messages: list[dict[str, object]] = []
    raw_tables = {
        "keyword": "团队周工作计划",
        "total": 30,
        "matched": 18,
        "truncated": True,
        "tables": [
            {"table_id": f"tbl{i:03d}", "name": f"团队周工作计划表 {i}"}
            for i in range(18)
        ],
    }

    builder.add_tool_result(messages, "call-1", "bitable_list_tables", json.dumps(raw_tables, ensure_ascii=False))

    payload = json.loads(str(messages[-1]["content"]))
    assert payload["total"] == 30
    assert payload["matched"] == 18
    assert payload["truncated_for_llm"] is True
    assert len(payload["tables"]) == 8
    assert payload["tables"][0]["name"] == "团队周工作计划表 0"


def test_add_tool_result_compacts_bitable_field_metadata_for_llm(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)
    messages: list[dict[str, object]] = []
    raw_fields = {
        "table_id": "tbl_week",
        "total": 25,
        "fields": [
            {
                "field_id": f"fld{i:03d}",
                "field_name": f"字段{i}",
                "type": 1,
                "property": {
                    "option_count": 10,
                    "options_preview": ["A", "B", "C", "D", "E"],
                    "formatter": "0.00",
                },
            }
            for i in range(25)
        ],
    }

    builder.add_tool_result(messages, "call-2", "bitable_list_fields", json.dumps(raw_fields, ensure_ascii=False))

    payload = json.loads(str(messages[-1]["content"]))
    assert payload["table_id"] == "tbl_week"
    assert payload["total"] == 25
    assert payload["truncated_for_llm"] is True
    assert len(payload["fields"]) == 12
    assert payload["fields"][0]["field_name"] == "字段0"
    assert payload["fields"][0]["property"]["option_count"] == 10


#endregion
