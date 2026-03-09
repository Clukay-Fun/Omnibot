import json

import pytest

from nanobot.agent.skill_runtime.table_profile_synthesizer import TableProfileSynthesizer
from nanobot.agent.tools.feishu_data.bitable import BitableSyncSchemaTool
from nanobot.agent.skill_runtime.table_registry import TableRegistry
from nanobot.config.schema import FeishuDataConfig
from nanobot.providers.base import LLMResponse


def test_table_registry_caches_profile_by_schema_hash(tmp_path):
    workspace_registry = tmp_path / "skills" / "table_registry.yaml"
    workspace_registry.parent.mkdir(parents=True, exist_ok=True)
    workspace_registry.write_text(
        """
version: 1
tables:
  weekly_plan:
    app_token: app_live
    table_id: tbl_week
    display_name: 团队周工作计划表
    aliases: [周计划, 周报表]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    registry = TableRegistry(workspace=tmp_path)
    fields = [
        {"field_name": "姓名", "type": 11, "property": {}},
        {"field_name": "周次", "type": 1, "property": {}},
        {"field_name": "工作内容", "type": 1, "property": {}},
    ]

    first = registry.get_table_profile("weekly_plan", table_name="团队周工作计划表", fields=fields)
    second = registry.get_table_profile("weekly_plan", table_name="团队周工作计划表", fields=fields)

    assert first["schema_hash"] == second["schema_hash"]
    cache_path = tmp_path / "memory" / "feishu" / "table_profile_cache.json"
    assert cache_path.exists()
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(payload["entries"]) == 1


def test_table_registry_invalidates_profile_when_schema_changes(tmp_path):
    workspace_registry = tmp_path / "skills" / "table_registry.yaml"
    workspace_registry.parent.mkdir(parents=True, exist_ok=True)
    workspace_registry.write_text(
        """
version: 1
tables:
  contract_registry:
    app_token: app_live
    table_id: tbl_contract
    display_name: 合同管理
""".strip()
        + "\n",
        encoding="utf-8",
    )
    registry = TableRegistry(workspace=tmp_path)
    fields_v1 = [
        {"field_name": "合同编号", "type": 1, "property": {}},
        {"field_name": "合同名称", "type": 1, "property": {}},
    ]
    fields_v2 = [
        {"field_name": "合同编号", "type": 1, "property": {}},
        {"field_name": "合同名称", "type": 1, "property": {}},
        {"field_name": "到期时间", "type": 5, "property": {}},
    ]

    first = registry.get_table_profile("contract_registry", table_name="合同管理", fields=fields_v1)
    second = registry.get_table_profile("contract_registry", table_name="合同管理", fields=fields_v2)

    assert first["schema_hash"] != second["schema_hash"]
    assert "到期时间" in second["time_fields"]
    payload = json.loads((tmp_path / "memory" / "feishu" / "table_profile_cache.json").read_text(encoding="utf-8"))
    assert len(payload["entries"]) == 2


def test_table_registry_infers_core_table_profile_from_schema(tmp_path):
    workspace_registry = tmp_path / "skills" / "table_registry.yaml"
    workspace_registry.parent.mkdir(parents=True, exist_ok=True)
    workspace_registry.write_text(
        """
version: 1
tables:
  case_registry:
    app_token: app_live
    table_id: tbl_case
    display_name: 案件项目总库
""".strip()
        + "\n",
        encoding="utf-8",
    )
    registry = TableRegistry(workspace=tmp_path)
    fields = [
        {"field_name": "案号", "type": 1, "property": {}},
        {"field_name": "项目ID", "type": 1, "property": {}},
        {"field_name": "主办律师", "type": 11, "property": {}},
        {"field_name": "下一节点时间", "type": 5, "property": {}},
        {"field_name": "案件状态", "type": 3, "property": {}},
    ]

    profile = registry.get_table_profile("case_registry", table_name="案件项目总库", fields=fields)

    assert profile["display_name"] == "案件项目总库"
    assert "案件" in profile["aliases"]
    assert profile["purpose_guess"]
    assert set(profile["identity_fields_guess"]) >= {"案号", "项目ID"}
    assert "主办律师" in profile["person_fields"]
    assert "下一节点时间" in profile["time_fields"]
    assert "案件状态" in profile["status_fields"]


class _FakeProfileProvider:
    def __init__(self):
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(
            content=json.dumps(
                {
                    "aliases": ["周计划", "本周工作"],
                    "purpose_guess": "记录团队每周工作安排与完成进度",
                    "common_query_patterns": ["查这周计划"],
                    "common_write_patterns": ["补写本周工作"],
                    "confidence": "high",
                },
                ensure_ascii=False,
            ),
            tool_calls=[],
        )

    def get_default_model(self) -> str:
        return "test-model"


@pytest.mark.asyncio
async def test_table_registry_synthesizes_profile_with_provider_and_caches(tmp_path):
    workspace_registry = tmp_path / "skills" / "table_registry.yaml"
    workspace_registry.parent.mkdir(parents=True, exist_ok=True)
    workspace_registry.write_text(
        """
version: 1
tables:
  weekly_plan:
    app_token: app_live
    table_id: tbl_week
    display_name: 团队周工作计划表
""".strip()
        + "\n",
        encoding="utf-8",
    )
    provider = _FakeProfileProvider()
    registry = TableRegistry(
        workspace=tmp_path,
        profile_synthesizer=TableProfileSynthesizer(provider=provider, model="test-model"),
    )
    fields = [
        {"field_name": "姓名", "type": 11, "property": {}},
        {"field_name": "周次", "type": 1, "property": {}},
        {"field_name": "工作内容", "type": 1, "property": {}},
    ]

    first = await registry.get_or_synthesize_table_profile("weekly_plan", table_name="团队周工作计划表", fields=fields)
    second = await registry.get_or_synthesize_table_profile("weekly_plan", table_name="团队周工作计划表", fields=fields)

    assert first["source"] == "llm"
    assert "周计划" in first["aliases"]
    assert first["purpose_guess"] == "记录团队每周工作安排与完成进度"
    assert second["source"] == "llm"
    assert len(provider.calls) == 1


class _FakeSyncClient:
    def __init__(self):
        self.calls = []

    async def request(self, method, path, params=None, json_body=None, headers=None, **kwargs):
        self.calls.append({"method": method, "path": path, "params": params, "json_body": json_body, **kwargs})
        if path.endswith("/tables"):
            return {"data": {"items": [{"table_id": "tbl_week", "name": "团队周工作计划表"}]}}
        return {
            "data": {
                "items": [
                    {"field_id": "fld_owner", "field_name": "姓名", "type": 11, "property": {}},
                    {"field_id": "fld_week", "field_name": "周次", "type": 1, "property": {}},
                    {"field_id": "fld_content", "field_name": "工作内容", "type": 1, "property": {}},
                ]
            }
        }


@pytest.mark.asyncio
async def test_schema_sync_prewarms_table_profile_cache(tmp_path):
    workspace_registry = tmp_path / "skills" / "table_registry.yaml"
    workspace_registry.parent.mkdir(parents=True, exist_ok=True)
    workspace_registry.write_text(
        """
version: 1
tables:
  weekly_plan:
    app_token: app_live
    table_id: tbl_week
    display_name: 团队周工作计划表
""".strip()
        + "\n",
        encoding="utf-8",
    )

    provider = _FakeProfileProvider()
    tool = BitableSyncSchemaTool(
        FeishuDataConfig(enabled=True),
        _FakeSyncClient(),
        workspace=tmp_path,
        profile_synthesizer=TableProfileSynthesizer(provider=provider, model="test-model"),
    )

    payload = json.loads(await tool.execute(app_token="app_live"))

    assert payload["tables"][0]["schema_hash"]
    assert payload["tables"][0]["profile"]["display_name"] == "团队周工作计划表"
    assert payload["tables"][0]["profile"]["source"] == "llm"
    cache_path = tmp_path / "memory" / "feishu" / "table_profile_cache.json"
    assert cache_path.exists()
