import json
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.tools.feishu_data.bitable_admin_tools import (
    BitableAppCreateTool,
    BitableTableCreateTool,
    BitableViewCreateTool,
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
async def test_bitable_app_create(config: FeishuDataConfig, client: AsyncMock) -> None:
    tool = BitableAppCreateTool(config, client)
    client.request.return_value = {"data": {"app_token": "app_new"}}

    result = json.loads(await tool.execute(name="CaseDB", time_zone="Asia/Shanghai"))

    assert result["app"]["app_token"] == "app_new"
    client.request.assert_called_once_with(
        "POST",
        FeishuEndpoints.bitable_apps(),
        json_body={"name": "CaseDB", "time_zone": "Asia/Shanghai"},
    )


@pytest.mark.asyncio
async def test_bitable_table_create(config: FeishuDataConfig, client: AsyncMock) -> None:
    tool = BitableTableCreateTool(config, client)
    client.request.return_value = {"data": {"table_id": "tbl_new"}}

    _ = await tool.execute(app_token="app_1", name="Tasks")

    client.request.assert_called_once_with(
        "POST",
        FeishuEndpoints.bitable_tables("app_1"),
        json_body={"table": {"name": "Tasks"}},
    )


@pytest.mark.asyncio
async def test_bitable_view_create(config: FeishuDataConfig, client: AsyncMock) -> None:
    tool = BitableViewCreateTool(config, client)
    client.request.return_value = {"data": {"view_id": "vew_new"}}

    _ = await tool.execute(app_token="app_1", table_id="tbl_1", view_name="Board", view_type="kanban")

    client.request.assert_called_once_with(
        "POST",
        FeishuEndpoints.bitable_views("app_1", "tbl_1"),
        json_body={"view_name": "Board", "view_type": "kanban"},
    )
