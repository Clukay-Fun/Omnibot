"""飞书写入工具的测试用例：两阶段安全机制（dry_run + confirm_token）。"""

import json
import time
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from nanobot.agent.tools.feishu_data.bitable import BitableMatchTableTool, BitablePrepareCreateTool
from nanobot.agent.tools.feishu_data.bitable_write import (
    BitableCreateTool,
    BitableDeleteTool,
    BitableUpdateTool,
)
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.confirm_store import ConfirmTokenStore
from nanobot.agent.tools.feishu_data.errors import FeishuDataAPIError
from nanobot.agent.skill_runtime.table_profile_synthesizer import TableProfileSynthesizer
from nanobot.agent.skill_runtime.table_registry import TableRegistry
from nanobot.config.schema import FeishuDataBitableConfig, FeishuDataConfig
from nanobot.providers.base import LLMResponse


@pytest.fixture
def mock_config():
    return FeishuDataConfig(
        enabled=True,
        app_id="test_id",
        app_secret="test_secret",
        bitable=FeishuDataBitableConfig(
            default_app_token="app123",
            default_table_id="tbl456",
        ),
    )


@pytest.fixture
def mock_client():
    return AsyncMock(spec=FeishuDataClient)


@pytest.fixture
def store():
    return ConfirmTokenStore(ttl_seconds=300)


class FakeUserTokenManager:
    def __init__(self, token: str = "oauth-token"):
        self.token = token
        self.calls: list[str] = []

    def get_valid_access_token(self, open_id: str) -> str:
        self.calls.append(open_id)
        return self.token


class FakeProfileProvider:
    async def chat(self, **kwargs):
        return LLMResponse(
            content=json.dumps(
                {
                    "aliases": ["本周总结"],
                    "purpose_guess": "记录团队每周计划和总结",
                    "common_query_patterns": ["查本周总结"],
                    "common_write_patterns": ["补本周总结"],
                    "confidence": "high",
                },
                ensure_ascii=False,
            ),
            tool_calls=[],
        )

    def get_default_model(self) -> str:
        return "test-model"


# --------------------------------------------------------------------------
# ConfirmTokenStore 单元测试
# --------------------------------------------------------------------------


def test_confirm_store_create_and_consume():
    """生成 token 后应能成功消费一次。"""
    store = ConfirmTokenStore(ttl_seconds=300)
    payload = {"action": "create", "fields": {"Name": "Alice"}}
    token = store.create(payload)

    assert store.consume(token, payload) is True
    # 二次消费应失败
    assert store.consume(token, payload) is False


def test_confirm_store_payload_mismatch():
    """当 payload 不匹配时应拒绝消费。"""
    store = ConfirmTokenStore(ttl_seconds=300)
    payload = {"action": "create", "fields": {"Name": "Alice"}}
    token = store.create(payload)

    wrong_payload = {"action": "create", "fields": {"Name": "Bob"}}
    assert store.consume(token, wrong_payload) is False


def test_confirm_store_expired_token():
    """过期的 token 应被拒绝。"""
    store = ConfirmTokenStore(ttl_seconds=0)  # 立即过期
    payload = {"action": "delete", "record_id": "rec123"}
    token = store.create(payload)

    time.sleep(0.01)
    assert store.consume(token, payload) is False


# --------------------------------------------------------------------------
# BitableCreateTool
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_dry_run(mock_config, mock_client, store):
    """不传 confirm_token 时应返回 dry_run 预览。"""
    tool = BitableCreateTool(config=mock_config, client=mock_client, store=store)
    mock_client.request.return_value = {"data": {"items": []}}
    res = json.loads(await tool.execute(fields={"Name": "Alice"}))

    assert res["dry_run"] is True
    assert "confirm_token" in res
    assert res["preview"]["action"] == "create"
    mock_client.request.assert_called_once()


@pytest.mark.asyncio
async def test_create_confirm(mock_config, mock_client, store):
    """传入正确 confirm_token 时应执行实际写入。"""
    tool = BitableCreateTool(config=mock_config, client=mock_client, store=store)
    mock_client.request.side_effect = [
        {"data": {"items": []}},
        {"data": {"record": {"record_id": "rec_new", "fields": {"Name": "Alice"}}}},
    ]

    # 阶段 1
    dry = json.loads(await tool.execute(fields={"Name": "Alice"}))
    token = dry["confirm_token"]

    # 阶段 2
    res = json.loads(await tool.execute(fields={"Name": "Alice"}, confirm_token=token))
    assert res["success"] is True
    assert res["record_id"] == "rec_new"
    assert mock_client.request.call_count == 2


@pytest.mark.asyncio
async def test_create_invalid_token(mock_config, mock_client, store):
    """无效 token 应被拒绝。"""
    tool = BitableCreateTool(config=mock_config, client=mock_client, store=store)
    mock_client.request.return_value = {"data": {"items": []}}
    res = json.loads(await tool.execute(fields={"Name": "Alice"}, confirm_token="bad_token"))
    assert "error" in res
    mock_client.request.assert_called_once()


@pytest.mark.asyncio
async def test_create_resolves_self_person_field_from_runtime_sender(mock_config, mock_client, store):
    tool = BitableCreateTool(config=mock_config, client=mock_client, store=store)
    tool.set_runtime_context("feishu", "ou_sender", sender_id="ou_sender", metadata={})
    mock_client.request.return_value = {
        "data": {
            "items": [
                {"field_id": "fld_person", "field_name": "人员", "type": 11, "property": {}},
            ]
        }
    }

    res = json.loads(await tool.execute(fields={"人员": "我"}))

    assert res["dry_run"] is True
    assert res["preview"]["fields"]["人员"] == [{"id": "ou_sender"}]
    mock_client.request.assert_called_once()


@pytest.mark.asyncio
async def test_create_resolves_named_person_field_from_app_contact_lookup(mock_config, mock_client, store, tmp_path):
    tool = BitableCreateTool(config=mock_config, client=mock_client, store=store, workspace=tmp_path)
    tool.set_runtime_context("feishu", "ou_sender", sender_id="ou_sender", metadata={})
    mock_client.request.side_effect = [
        {
            "data": {
                "items": [
                    {"field_id": "fld_person", "field_name": "人员", "type": 11, "property": {}},
                ]
            }
        },
        {
            "data": {
                "items": [
                    {"name": "房怡康", "open_id": "ou_fang"},
                ]
            }
        },
    ]

    res = json.loads(await tool.execute(fields={"人员": "房怡康"}))

    assert res["dry_run"] is True
    assert res["preview"]["fields"]["人员"] == [{"id": "ou_fang"}]
    assert mock_client.request.call_count == 2


@pytest.mark.asyncio
async def test_create_resolves_named_person_field_with_user_oauth_fallback(mock_config, mock_client, store, tmp_path):
    token_manager = FakeUserTokenManager()
    tool = BitableCreateTool(
        config=mock_config,
        client=mock_client,
        store=store,
        workspace=tmp_path,
        user_token_manager=cast(Any, token_manager),
    )
    tool.set_runtime_context("feishu", "ou_sender", sender_id="ou_sender", metadata={})
    mock_client.request.side_effect = [
        {
            "data": {
                "items": [
                    {"field_id": "fld_person", "field_name": "人员", "type": 11, "property": {}},
                ]
            }
        },
        FeishuDataAPIError(403, "permission denied"),
        {
            "data": {
                "items": [
                    {"name": "房怡康", "open_id": "ou_fang"},
                ]
            }
        },
    ]

    res = json.loads(await tool.execute(fields={"人员": "房怡康"}))

    assert res["dry_run"] is True
    assert res["preview"]["fields"]["人员"] == [{"id": "ou_fang"}]
    assert token_manager.calls == ["ou_sender"]
    assert mock_client.request.call_args_list[2].kwargs["auth_mode"] == "user"
    assert mock_client.request.call_args_list[2].kwargs["bearer_token"] == "oauth-token"


@pytest.mark.asyncio
async def test_create_normalizes_amount_date_and_status_fields(mock_config, mock_client, store):
    tool = BitableCreateTool(config=mock_config, client=mock_client, store=store)
    mock_client.request.return_value = {
        "data": {
            "items": [
                {"field_id": "fld_amount", "field_name": "合同金额", "type": 2, "property": {}},
                {"field_id": "fld_expiry", "field_name": "到期时间", "type": 5, "property": {}},
                {
                    "field_id": "fld_status",
                    "field_name": "合同状态",
                    "type": 3,
                    "property": {"options": [{"name": "草稿"}, {"name": "已签署"}]},
                },
            ]
        }
    }

    res = json.loads(
        await tool.execute(fields={"合同金额": "12万", "到期时间": "2026年6月30日", "合同状态": "已签"})
    )

    expected_ms = int(datetime(2026, 6, 30, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC).timestamp() * 1000)
    assert res["dry_run"] is True
    assert res["preview"]["fields"]["合同金额"] == 120000
    assert res["preview"]["fields"]["到期时间"] == expected_ms
    assert res["preview"]["fields"]["合同状态"] == "已签署"


# --------------------------------------------------------------------------
# BitableUpdateTool
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_two_phase(mock_config, mock_client, store):
    """完整的两阶段更新流程。"""
    tool = BitableUpdateTool(config=mock_config, client=mock_client, store=store)
    mock_client.request.return_value = {
        "data": {"record": {"record_id": "rec001", "fields": {"Status": "Done"}}}
    }

    dry = json.loads(await tool.execute(record_id="rec001", fields={"Status": "Done"}))
    assert dry["dry_run"] is True

    res = json.loads(await tool.execute(
        record_id="rec001", fields={"Status": "Done"}, confirm_token=dry["confirm_token"]
    ))
    assert res["success"] is True


# --------------------------------------------------------------------------
# BitableDeleteTool
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_two_phase(mock_config, mock_client, store):
    """完整的两阶段删除流程。"""
    tool = BitableDeleteTool(config=mock_config, client=mock_client, store=store)
    mock_client.request.return_value = {}

    dry = json.loads(await tool.execute(record_id="rec_del"))
    assert dry["dry_run"] is True

    res = json.loads(await tool.execute(record_id="rec_del", confirm_token=dry["confirm_token"]))
    assert res["success"] is True
    assert res["deleted_record_id"] == "rec_del"


@pytest.mark.asyncio
async def test_match_table_ranks_live_candidates_without_hardcoded_phrase(mock_config, mock_client):
    tool = BitableMatchTableTool(config=mock_config, client=mock_client)
    mock_client.request.return_value = {
        "data": {
            "items": [
                {"table_id": "tbl_week", "name": "团队周工作计划表"},
                {"table_id": "tbl_week_history", "name": "团队周工作计划历史"},
                {"table_id": "tbl_contract", "name": "合同台账"},
            ]
        }
    }

    res = json.loads(await tool.execute(query="需要新增工作记录到团队周工作计划表里回复房怡康"))

    assert res["best_match"]["table_id"] == "tbl_week"
    assert res["candidates"][0]["score"] >= res["candidates"][1]["score"]
    assert "normalized_substring" in res["candidates"][0]["reasons"]


@pytest.mark.asyncio
async def test_match_table_prefers_registered_core_table_aliases(mock_config, mock_client, tmp_path):
    workspace_registry = tmp_path / "skills" / "table_registry.yaml"
    workspace_registry.parent.mkdir(parents=True, exist_ok=True)
    workspace_registry.write_text(
        """
version: 1
tables:
  weekly_plan:
    app_token: app123
    table_id: tbl_week
    display_name: 团队周工作计划表
    aliases: [周计划, 周报表, 本周工作]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    tool = BitableMatchTableTool(config=mock_config, client=mock_client, workspace=tmp_path)
    mock_client.request.return_value = {
        "data": {
            "items": [
                {"table_id": "tbl_week", "name": "团队计划主表"},
                {"table_id": "tbl_contract", "name": "合同台账"},
            ]
        }
    }

    res = json.loads(await tool.execute(query="帮我补一下本周工作", app_token="app123"))

    assert res["best_match"]["table_id"] == "tbl_week"
    assert "profile_alias_substring" in res["best_match"]["reasons"]


@pytest.mark.asyncio
async def test_match_table_uses_cached_synthesized_profile_aliases(mock_config, mock_client, tmp_path):
    workspace_registry = tmp_path / "skills" / "table_registry.yaml"
    workspace_registry.parent.mkdir(parents=True, exist_ok=True)
    workspace_registry.write_text(
        """
version: 1
tables:
  weekly_plan:
    app_token: app123
    table_id: tbl_week
    display_name: 团队周工作计划表
""".strip()
        + "\n",
        encoding="utf-8",
    )
    registry = TableRegistry(
        workspace=tmp_path,
        profile_synthesizer=TableProfileSynthesizer(provider=FakeProfileProvider(), model="test-model"),
    )
    await registry.get_or_synthesize_table_profile(
        "weekly_plan",
        table_name="团队周工作计划表",
        fields=[
            {"field_name": "姓名", "type": 11, "property": {}},
            {"field_name": "周次", "type": 1, "property": {}},
            {"field_name": "工作内容", "type": 1, "property": {}},
        ],
    )

    tool = BitableMatchTableTool(config=mock_config, client=mock_client, workspace=tmp_path)
    mock_client.request.return_value = {
        "data": {
            "items": [
                {"table_id": "tbl_week", "name": "团队计划主表"},
                {"table_id": "tbl_contract", "name": "合同台账"},
            ]
        }
    }

    res = json.loads(await tool.execute(query="帮我补一下本周总结", app_token="app123"))

    assert res["best_match"]["table_id"] == "tbl_week"
    assert res["best_match"]["profile"]["source"] == "llm"
    assert "profile_alias_substring" in res["best_match"]["reasons"]


@pytest.mark.asyncio
async def test_match_table_bubbles_list_table_error(mock_config, mock_client):
    tool = BitableMatchTableTool(config=mock_config, client=mock_client)
    mock_client.request.side_effect = RuntimeError("table listing failed")

    res = json.loads(await tool.execute(query="记到团队周工作计划表"))

    assert res["error"] == "table listing failed"
    assert res["candidates"] == []


@pytest.mark.asyncio
async def test_prepare_create_returns_selected_table_and_compact_schema(mock_config, mock_client):
    tool = BitablePrepareCreateTool(config=mock_config, client=mock_client)
    mock_client.request.side_effect = [
        {
            "data": {
                "items": [
                    {"table_id": "tbl_week", "name": "团队周工作计划表"},
                    {"table_id": "tbl_contract", "name": "合同台账"},
                ]
            }
        },
        {
            "data": {
                "items": [
                    {"field_id": "fld_owner", "field_name": "负责人", "type": 1, "property": {}},
                    {"field_id": "fld_content", "field_name": "工作内容", "type": 1, "property": {}},
                    {
                        "field_id": "fld_status",
                        "field_name": "状态",
                        "type": 3,
                        "property": {"options": [{"id": "1", "name": "进行中"}, {"id": "2", "name": "已完成"}]},
                    },
                ]
            }
        },
    ]

    res = json.loads(await tool.execute(request_text="需要新增工作记录到团队周工作计划表里回复房怡康"))

    assert res["needs_table_confirmation"] is False
    assert res["selected_table"]["table_id"] == "tbl_week"
    assert res["next_step"]["tool"] == "bitable_create"
    assert res["next_step"]["arguments"]["table_id"] == "tbl_week"
    assert "负责人" in res["suggested_field_names"]
    assert res["fields"][2]["property"]["option_count"] == 2


@pytest.mark.asyncio
async def test_prepare_create_includes_profile_for_registered_core_table(mock_config, mock_client, tmp_path):
    workspace_registry = tmp_path / "skills" / "table_registry.yaml"
    workspace_registry.parent.mkdir(parents=True, exist_ok=True)
    workspace_registry.write_text(
        """
version: 1
tables:
  weekly_plan:
    app_token: app123
    table_id: tbl_week
    display_name: 团队周工作计划表
    aliases: [周计划, 周报表, 本周工作]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    tool = BitablePrepareCreateTool(config=mock_config, client=mock_client, workspace=tmp_path)
    mock_client.request.side_effect = [
        {
            "data": {
                "items": [
                    {"table_id": "tbl_week", "name": "团队计划主表"},
                    {"table_id": "tbl_contract", "name": "合同台账"},
                ]
            }
        },
        {
            "data": {
                "items": [
                    {"field_id": "fld_owner", "field_name": "姓名", "type": 11, "property": {}},
                    {"field_id": "fld_week", "field_name": "周次", "type": 1, "property": {}},
                    {"field_id": "fld_content", "field_name": "工作内容", "type": 1, "property": {}},
                ]
            }
        },
    ]

    res = json.loads(await tool.execute(request_text="帮我补一下本周工作", app_token="app123"))

    assert res["selected_table"]["table_id"] == "tbl_week"
    assert res["profile"]["display_name"] == "团队周工作计划表"
    assert "周计划" in res["profile"]["aliases"]
    assert set(res["profile"]["identity_fields_guess"]) >= {"姓名", "周次"}


@pytest.mark.asyncio
async def test_prepare_create_prefills_weekly_plan_fields_from_request(mock_config, mock_client, tmp_path):
    workspace_registry = tmp_path / "skills" / "table_registry.yaml"
    workspace_registry.parent.mkdir(parents=True, exist_ok=True)
    workspace_registry.write_text(
        """
version: 1
tables:
  weekly_plan:
    app_token: app123
    table_id: tbl_week
    display_name: 团队周工作计划表
    aliases: [周计划, 周报表, 本周工作]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    tool = BitablePrepareCreateTool(config=mock_config, client=mock_client, workspace=tmp_path)
    mock_client.request.side_effect = [
        {"data": {"items": [{"table_id": "tbl_week", "name": "团队计划主表"}]}},
        {
            "data": {
                "items": [
                    {"field_id": "fld_owner", "field_name": "姓名", "type": 11, "property": {}},
                    {"field_id": "fld_week", "field_name": "周次", "type": 1, "property": {}},
                    {"field_id": "fld_content", "field_name": "工作内容", "type": 1, "property": {}},
                ]
            }
        },
        {
            "data": {
                "items": [
                    {"record_id": "rec_week_1", "fields": {"姓名": "房怡康", "周次": "本周", "工作内容": "旧内容"}}
                ]
            }
        },
    ]

    res = json.loads(await tool.execute(request_text="帮我给房怡康补一下本周工作计划：整理合同台账", app_token="app123"))

    assert res["draft_fields"]["姓名"] == "房怡康"
    assert res["draft_fields"]["周次"] == "本周"
    assert res["draft_fields"]["工作内容"] == "整理合同台账"
    assert res["operation_guess"] == "update_existing"
    assert res["record_lookup"]["matched"] == 1
    assert res["record_lookup"]["records"][0]["record_id"] == "rec_week_1"
    assert res["next_step"]["tool"] == "bitable_update"
    assert res["next_step"]["arguments"]["record_id"] == "rec_week_1"
    assert res["next_step"]["arguments"]["fields"] == {"工作内容": "整理合同台账"}
    assert res["missing_identity_fields"] == []


@pytest.mark.asyncio
async def test_prepare_create_prefills_contract_fields_from_request(mock_config, mock_client, tmp_path):
    workspace_registry = tmp_path / "skills" / "table_registry.yaml"
    workspace_registry.parent.mkdir(parents=True, exist_ok=True)
    workspace_registry.write_text(
        """
version: 1
tables:
  contract_registry:
    app_token: app123
    table_id: tbl_contract
    display_name: 合同管理
    aliases: [合同管理, 合同台账]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    tool = BitablePrepareCreateTool(config=mock_config, client=mock_client, workspace=tmp_path)
    mock_client.request.side_effect = [
        {"data": {"items": [{"table_id": "tbl_contract", "name": "合同主表"}]}},
        {
            "data": {
                "items": [
                    {"field_id": "fld_no", "field_name": "合同编号", "type": 1, "property": {}},
                    {"field_id": "fld_vendor", "field_name": "乙方", "type": 1, "property": {}},
                    {"field_id": "fld_amount", "field_name": "合同金额", "type": 2, "property": {}},
                    {"field_id": "fld_expiry", "field_name": "到期时间", "type": 5, "property": {}},
                ]
            }
        },
        {"data": {"items": []}},
    ]

    res = json.loads(
        await tool.execute(
            request_text="新增合同，合同编号 HT-001，乙方 星火科技，合同金额 120000，到期时间 2026-06-30",
            app_token="app123",
        )
    )

    assert res["draft_fields"]["合同编号"] == "HT-001"
    assert res["draft_fields"]["乙方"] == "星火科技"
    assert res["draft_fields"]["合同金额"] == "120000"
    assert res["draft_fields"]["到期时间"] == "2026-06-30"
    assert res["operation_guess"] == "create_new"
    assert res["record_lookup"]["matched"] == 0
    assert res["next_step"]["tool"] == "bitable_create"
    assert res["next_step"]["arguments"]["fields"]["合同编号"] == "HT-001"


@pytest.mark.asyncio
async def test_prepare_create_normalizes_contract_amount_date_and_status(mock_config, mock_client, tmp_path):
    workspace_registry = tmp_path / "skills" / "table_registry.yaml"
    workspace_registry.parent.mkdir(parents=True, exist_ok=True)
    workspace_registry.write_text(
        """
version: 1
tables:
  contract_registry:
    app_token: app123
    table_id: tbl_contract
    display_name: 合同管理
    aliases: [合同管理, 合同台账]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    tool = BitablePrepareCreateTool(config=mock_config, client=mock_client, workspace=tmp_path)
    mock_client.request.side_effect = [
        {"data": {"items": [{"table_id": "tbl_contract", "name": "合同主表"}]}},
        {
            "data": {
                "items": [
                    {"field_id": "fld_no", "field_name": "合同编号", "type": 1, "property": {}},
                    {"field_id": "fld_amount", "field_name": "合同金额", "type": 2, "property": {}},
                    {"field_id": "fld_expiry", "field_name": "到期时间", "type": 5, "property": {}},
                    {
                        "field_id": "fld_status",
                        "field_name": "合同状态",
                        "type": 3,
                        "property": {"options": [{"name": "草稿"}, {"name": "已签署"}]},
                    },
                ]
            }
        },
        {"data": {"items": []}},
    ]

    res = json.loads(
        await tool.execute(
            request_text="新增合同，合同编号 HT-001，合同金额 12万，到期时间 2026年6月30日，合同状态 已签",
            app_token="app123",
        )
    )

    assert res["draft_fields"]["合同金额"] == "120000"
    assert res["draft_fields"]["到期时间"] == "2026-06-30"
    assert res["draft_fields"]["合同状态"] == "已签署"


@pytest.mark.asyncio
async def test_prepare_create_prefills_case_fields_from_request(mock_config, mock_client, tmp_path):
    workspace_registry = tmp_path / "skills" / "table_registry.yaml"
    workspace_registry.parent.mkdir(parents=True, exist_ok=True)
    workspace_registry.write_text(
        """
version: 1
tables:
  case_registry:
    app_token: app123
    table_id: tbl_case
    display_name: 案件项目总库
    aliases: [案件项目总库, 案件库, 项目总库]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    tool = BitablePrepareCreateTool(config=mock_config, client=mock_client, workspace=tmp_path)
    mock_client.request.side_effect = [
        {"data": {"items": [{"table_id": "tbl_case", "name": "案件主表"}]}},
        {
            "data": {
                "items": [
                    {"field_id": "fld_case_no", "field_name": "案号", "type": 1, "property": {}},
                    {"field_id": "fld_case_id", "field_name": "项目ID", "type": 1, "property": {}},
                    {"field_id": "fld_owner", "field_name": "主办律师", "type": 11, "property": {}},
                    {"field_id": "fld_status", "field_name": "案件状态", "type": 3, "property": {}},
                ]
            }
        },
        {"data": {"items": []}},
    ]

    res = json.loads(
        await tool.execute(
            request_text="更新案件，案号 (2026)京01民初123号，项目ID CASE-001，主办律师 房怡康，案件状态 已立案",
            app_token="app123",
        )
    )

    assert res["draft_fields"]["案号"] == "(2026)京01民初123号"
    assert res["draft_fields"]["项目ID"] == "CASE-001"
    assert res["draft_fields"]["主办律师"] == "房怡康"
    assert res["draft_fields"]["案件状态"] == "已立案"


@pytest.mark.asyncio
async def test_prepare_create_switches_case_flow_to_update_when_identity_matches_existing_record(mock_config, mock_client, tmp_path):
    workspace_registry = tmp_path / "skills" / "table_registry.yaml"
    workspace_registry.parent.mkdir(parents=True, exist_ok=True)
    workspace_registry.write_text(
        """
version: 1
tables:
  case_registry:
    app_token: app123
    table_id: tbl_case
    display_name: 案件项目总库
    aliases: [案件项目总库, 案件库, 项目总库]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    tool = BitablePrepareCreateTool(config=mock_config, client=mock_client, workspace=tmp_path)
    mock_client.request.side_effect = [
        {"data": {"items": [{"table_id": "tbl_case", "name": "案件主表"}]}},
        {
            "data": {
                "items": [
                    {"field_id": "fld_case_no", "field_name": "案号", "type": 1, "property": {}},
                    {"field_id": "fld_case_id", "field_name": "项目ID", "type": 1, "property": {}},
                    {"field_id": "fld_owner", "field_name": "主办律师", "type": 11, "property": {}},
                    {"field_id": "fld_status", "field_name": "案件状态", "type": 3, "property": {}},
                ]
            }
        },
        {
            "data": {
                "items": [
                    {"record_id": "rec_case_1", "fields": {"案号": "(2026)京01民初123号", "项目ID": "CASE-001", "案件状态": "进行中"}}
                ]
            }
        },
    ]

    res = json.loads(
        await tool.execute(
            request_text="更新案件，案号 (2026)京01民初123号，项目ID CASE-001，主办律师 房怡康，案件状态 已立案",
            app_token="app123",
        )
    )

    assert res["operation_guess"] == "update_existing"
    assert res["next_step"]["tool"] == "bitable_update"
    assert res["next_step"]["arguments"]["record_id"] == "rec_case_1"
    assert res["next_step"]["arguments"]["fields"] == {"主办律师": "房怡康", "案件状态": "已立案"}


@pytest.mark.asyncio
async def test_prepare_create_uses_single_case_number_as_valid_identity_strategy(mock_config, mock_client, tmp_path):
    workspace_registry = tmp_path / "skills" / "table_registry.yaml"
    workspace_registry.parent.mkdir(parents=True, exist_ok=True)
    workspace_registry.write_text(
        """
version: 1
tables:
  case_registry:
    app_token: app123
    table_id: tbl_case
    display_name: 案件项目总库
    aliases: [案件项目总库, 案件库, 项目总库]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    tool = BitablePrepareCreateTool(config=mock_config, client=mock_client, workspace=tmp_path)
    mock_client.request.side_effect = [
        {"data": {"items": [{"table_id": "tbl_case", "name": "案件主表"}]}},
        {
            "data": {
                "items": [
                    {"field_id": "fld_case_no", "field_name": "案号", "type": 1, "property": {}},
                    {"field_id": "fld_case_id", "field_name": "项目ID", "type": 1, "property": {}},
                    {"field_id": "fld_status", "field_name": "案件状态", "type": 3, "property": {}},
                ]
            }
        },
        {
            "data": {
                "items": [
                    {"record_id": "rec_case_2", "fields": {"案号": "(2026)京01民初123号", "案件状态": "进行中"}}
                ]
            }
        },
    ]

    res = json.loads(
        await tool.execute(
            request_text="更新案件，案号 (2026)京01民初123号，案件状态 已立案",
            app_token="app123",
        )
    )

    assert res["identity_strategy"] == ["案号"]
    assert res["missing_identity_fields"] == []
    assert res["operation_guess"] == "update_existing"
    assert res["next_step"]["arguments"]["record_id"] == "rec_case_2"
    assert res["next_step"]["arguments"]["fields"] == {"案件状态": "已立案"}


@pytest.mark.asyncio
async def test_prepare_create_requires_week_for_weekly_plan_identity_strategy(mock_config, mock_client, tmp_path):
    workspace_registry = tmp_path / "skills" / "table_registry.yaml"
    workspace_registry.parent.mkdir(parents=True, exist_ok=True)
    workspace_registry.write_text(
        """
version: 1
tables:
  weekly_plan:
    app_token: app123
    table_id: tbl_week
    display_name: 团队周工作计划表
    aliases: [周计划, 周报表, 本周工作]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    tool = BitablePrepareCreateTool(config=mock_config, client=mock_client, workspace=tmp_path)
    mock_client.request.side_effect = [
        {"data": {"items": [{"table_id": "tbl_week", "name": "团队计划主表"}]}},
        {
            "data": {
                "items": [
                    {"field_id": "fld_owner", "field_name": "姓名", "type": 11, "property": {}},
                    {"field_id": "fld_week", "field_name": "周次", "type": 1, "property": {}},
                    {"field_id": "fld_content", "field_name": "工作内容", "type": 1, "property": {}},
                ]
            }
        },
    ]

    res = json.loads(await tool.execute(request_text="帮我给房怡康补一下工作计划：整理合同台账", app_token="app123"))

    assert res["identity_strategy"] == ["姓名", "周次"]
    assert res["missing_identity_fields"] == ["周次"]
    assert res["record_lookup"]["attempted"] is False


@pytest.mark.asyncio
async def test_prepare_create_requires_record_confirmation_when_identity_matches_multiple_records(mock_config, mock_client, tmp_path):
    workspace_registry = tmp_path / "skills" / "table_registry.yaml"
    workspace_registry.parent.mkdir(parents=True, exist_ok=True)
    workspace_registry.write_text(
        """
version: 1
tables:
  contract_registry:
    app_token: app123
    table_id: tbl_contract
    display_name: 合同管理
    aliases: [合同管理, 合同台账]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    tool = BitablePrepareCreateTool(config=mock_config, client=mock_client, workspace=tmp_path)
    mock_client.request.side_effect = [
        {"data": {"items": [{"table_id": "tbl_contract", "name": "合同主表"}]}},
        {
            "data": {
                "items": [
                    {"field_id": "fld_no", "field_name": "合同编号", "type": 1, "property": {}},
                    {"field_id": "fld_vendor", "field_name": "乙方", "type": 1, "property": {}},
                    {"field_id": "fld_status", "field_name": "合同状态", "type": 3, "property": {}},
                ]
            }
        },
        {
            "data": {
                "items": [
                    {"record_id": "rec_contract_1", "fields": {"合同编号": "HT-001", "乙方": "星火科技"}},
                    {"record_id": "rec_contract_2", "fields": {"合同编号": "HT-001", "乙方": "星火科技（旧）"}},
                ]
            }
        },
    ]

    res = json.loads(
        await tool.execute(
            request_text="更新合同，合同编号 HT-001，乙方 星火科技，合同状态 已签署",
            app_token="app123",
        )
    )

    assert res["operation_guess"] == "ambiguous_existing"
    assert res["needs_record_confirmation"] is True
    assert res["record_lookup"]["matched"] == 2
    assert res["next_step"] is None


@pytest.mark.asyncio
async def test_prepare_create_bubbles_field_fetch_error(mock_config, mock_client):
    tool = BitablePrepareCreateTool(config=mock_config, client=mock_client)
    mock_client.request.side_effect = [
        {
            "data": {
                "items": [
                    {"table_id": "tbl_week", "name": "团队周工作计划表"},
                ]
            }
        },
        RuntimeError("field schema failed"),
    ]

    res = json.loads(await tool.execute(request_text="需要新增工作记录到团队周工作计划表里回复房怡康"))

    assert res["needs_table_confirmation"] is False
    assert res["selected_table"]["table_id"] == "tbl_week"
    assert res["error"] == "field schema failed"
    assert res["fields"] == []


@pytest.mark.asyncio
async def test_prepare_create_returns_candidates_when_match_is_ambiguous(mock_config, mock_client):
    tool = BitablePrepareCreateTool(config=mock_config, client=mock_client)
    mock_client.request.return_value = {
        "data": {
            "items": [
                {"table_id": "tbl_week", "name": "团队周工作计划表"},
                {"table_id": "tbl_week_history", "name": "团队周工作计划历史"},
            ]
        }
    }

    res = json.loads(await tool.execute(request_text="帮我记到团队周工作计划里"))

    assert res["needs_table_confirmation"] is True
    assert len(res["candidates"]) == 2
    assert "bitable_create" not in str(res)


# --------------------------------------------------------------------------
# Registry 工具数量验证
# --------------------------------------------------------------------------


def test_registry_contains_all_feishu_data_tools():
    from nanobot.agent.tools.feishu_data.registry import build_feishu_data_tools

    config = FeishuDataConfig(enabled=True, app_id="id", app_secret="secret")
    tools = list(build_feishu_data_tools(config))
    assert len(tools) == 30

    names = {t.name for t in tools}
    assert names == {
        "bitable_search",
        "bitable_directory_search",
        "bitable_list_tables",
        "bitable_match_table",
        "bitable_list_fields",
        "bitable_prepare_create",
        "bitable_sync_schema",
        "bitable_get",
        "bitable_search_person",
        "doc_search",
        "bitable_create",
        "bitable_update",
        "bitable_delete",
        "bitable_app_create",
        "bitable_table_create",
        "bitable_view_create",
        "calendar_list",
        "calendar_create",
        "calendar_update",
        "calendar_delete",
        "calendar_freebusy",
        "task_create",
        "task_get",
        "task_update",
        "task_delete",
        "task_list",
        "tasklist_list",
        "subtask_create",
        "task_comment_add",
        "message_history_list",
    }


@pytest.mark.asyncio
async def test_create_normalizes_date_field_to_shanghai_midnight(mock_config, mock_client, store):
    tool = BitableCreateTool(config=mock_config, client=mock_client, store=store)
    mock_client.request.return_value = {"data": {"items": []}}

    res = json.loads(await tool.execute(fields={"截止日期": "2026-03-05", "Name": "Alice"}))

    expected = int(datetime(2026, 3, 4, 16, 0, tzinfo=UTC).timestamp() * 1000)
    assert res["preview"]["fields"]["截止日期"] == expected
    assert res["preview"]["fields"]["Name"] == "Alice"


@pytest.mark.asyncio
async def test_update_normalizes_numeric_date_field_before_confirmed_write(mock_config, mock_client, store):
    tool = BitableUpdateTool(config=mock_config, client=mock_client, store=store)
    mock_client.request.return_value = {
        "data": {"record": {"record_id": "rec001", "fields": {"截止日期": 1736035200000}}}
    }

    dry = json.loads(await tool.execute(record_id="rec001", fields={"截止日期": 1736035200}))
    token = dry["confirm_token"]
    await tool.execute(record_id="rec001", fields={"截止日期": 1736035200}, confirm_token=token)

    call = mock_client.request.call_args
    assert call is not None
    normalized_value = call.kwargs["json_body"]["fields"]["截止日期"]
    dt = datetime.fromtimestamp(normalized_value / 1000, tz=UTC).astimezone(ZoneInfo("Asia/Shanghai"))
    assert dt.hour == 0
    assert dt.minute == 0
