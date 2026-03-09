import json
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.tools.feishu_data.calendar_tools import (
    CalendarCreateTool,
    CalendarDeleteTool,
    CalendarFreebusyTool,
    CalendarListTool,
)
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.endpoints import FeishuEndpoints
from nanobot.agent.tools.feishu_data.registry import build_feishu_data_tools
from nanobot.agent.tools.registry import ToolExposureContext, ToolRegistry
from nanobot.config.schema import FeishuDataConfig


@pytest.fixture
def config() -> FeishuDataConfig:
    return FeishuDataConfig(enabled=True, app_id="id", app_secret="secret")


@pytest.fixture
def client() -> AsyncMock:
    return AsyncMock(spec=FeishuDataClient)


@pytest.mark.asyncio
async def test_calendar_list_uses_endpoint_and_params(config: FeishuDataConfig, client: AsyncMock) -> None:
    tool = CalendarListTool(config, client)
    client.request.return_value = {"data": {"items": [{"calendar_id": "cal_1"}], "has_more": False}}

    result = json.loads(await tool.execute(page_size=20, page_token="next"))

    assert result["items"][0]["calendar_id"] == "cal_1"
    client.request.assert_called_once_with(
        "GET",
        FeishuEndpoints.calendar_list(),
        params={"page_size": 20, "page_token": "next"},
    )


@pytest.mark.asyncio
async def test_calendar_create_builds_body(config: FeishuDataConfig, client: AsyncMock) -> None:
    tool = CalendarCreateTool(config, client)
    client.request.return_value = {"data": {"calendar_id": "cal_new"}}

    result = json.loads(await tool.execute(summary="Team", description="Ops", color=3))

    assert result["calendar"]["calendar_id"] == "cal_new"
    client.request.assert_called_once_with(
        "POST",
        FeishuEndpoints.calendar_list(),
        json_body={"summary": "Team", "description": "Ops", "color": 3},
    )


@pytest.mark.asyncio
async def test_calendar_freebusy_builds_payload(config: FeishuDataConfig, client: AsyncMock) -> None:
    tool = CalendarFreebusyTool(config, client)
    client.request.return_value = {"data": {"free_busy": []}}

    _ = await tool.execute(
        time_min="2026-03-01T00:00:00Z",
        time_max="2026-03-02T00:00:00Z",
        calendar_ids=["cal_1", "cal_2"],
    )

    client.request.assert_called_once_with(
        "POST",
        FeishuEndpoints.calendar_freebusy(),
        json_body={
            "time_min": "2026-03-01T00:00:00Z",
            "time_max": "2026-03-02T00:00:00Z",
            "calendars": [{"calendar_id": "cal_1"}, {"calendar_id": "cal_2"}],
        },
    )


@pytest.mark.asyncio
async def test_calendar_delete_requires_calendar_id(config: FeishuDataConfig, client: AsyncMock) -> None:
    tool = CalendarDeleteTool(config, client)

    result = json.loads(await tool.execute())

    assert "calendar_id" in result["error"]
    client.request.assert_not_called()


def test_registry_feature_flags_disable_optional_feishu_tools() -> None:
    config = FeishuDataConfig(
        enabled=True,
        app_id="id",
        app_secret="secret",
        feature_flags={
            "calendar_enabled": False,
            "task_enabled": False,
            "bitable_admin_enabled": False,
            "message_history_enabled": False,
        },
    )

    tool_names = {tool.name for tool in build_feishu_data_tools(config)}

    assert "bitable_search" in tool_names
    assert "doc_search" in tool_names
    assert "calendar_list" not in tool_names
    assert "task_create" not in tool_names
    assert "bitable_app_create" not in tool_names
    assert "message_history_list" not in tool_names


@pytest.mark.parametrize("content", ["搜索飞书文档里的日报模板", "搜一下云文档有哪些文档"])
def test_feishu_query_mode_exposes_doc_search_for_doc_requests(config: FeishuDataConfig, content: str) -> None:
    registry = ToolRegistry()
    for tool in build_feishu_data_tools(config):
        registry.register(tool)

    definitions = registry.get_definitions(
        ToolExposureContext(
            channel="feishu",
            user_text=content,
            mode="main_feishu_query",
        )
    )

    names = {tool["function"]["name"] for tool in definitions}
    assert "doc_search" in names
