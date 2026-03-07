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
