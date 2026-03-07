import json
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.endpoints import FeishuEndpoints
from nanobot.agent.tools.feishu_data.task_tools import (
    SubtaskCreateTool,
    TaskCommentAddTool,
    TaskCreateTool,
    TaskDeleteTool,
    TaskGetTool,
    TasklistListTool,
    TaskListTool,
    TaskUpdateTool,
)
from nanobot.config.schema import FeishuDataConfig


@pytest.fixture
def config() -> FeishuDataConfig:
    return FeishuDataConfig(enabled=True, app_id="id", app_secret="secret")


@pytest.fixture
def client() -> AsyncMock:
    return AsyncMock(spec=FeishuDataClient)


@pytest.mark.asyncio
async def test_task_create_payload(config: FeishuDataConfig, client: AsyncMock) -> None:
    tool = TaskCreateTool(config, client)
    client.request.return_value = {"data": {"task_id": "task_1"}}

    result = json.loads(await tool.execute(summary="Draft", description="Prepare"))

    assert result["task"]["task_id"] == "task_1"
    client.request.assert_called_once_with(
        "POST",
        FeishuEndpoints.task_v2_tasks(),
        json_body={"summary": "Draft", "description": "Prepare"},
    )


@pytest.mark.asyncio
async def test_task_get_path(config: FeishuDataConfig, client: AsyncMock) -> None:
    tool = TaskGetTool(config, client)
    client.request.return_value = {"data": {"task_id": "task_2"}}

    _ = await tool.execute(task_id="task_2")

    client.request.assert_called_once_with("GET", FeishuEndpoints.task_v2_task("task_2"))


@pytest.mark.asyncio
async def test_task_update_payload(config: FeishuDataConfig, client: AsyncMock) -> None:
    tool = TaskUpdateTool(config, client)
    client.request.return_value = {"data": {"task_id": "task_3"}}

    _ = await tool.execute(task_id="task_3", status="completed")

    client.request.assert_called_once()
    assert client.request.call_args.args == ("PATCH", FeishuEndpoints.task_v2_task("task_3"))
    body = client.request.call_args.kwargs["json_body"]
    assert body["update_fields"] == ["completed_at"]
    completed_at = body["task"]["completed_at"]
    assert isinstance(completed_at, str)
    assert completed_at.isdigit()
    assert int(completed_at) > 0


@pytest.mark.asyncio
async def test_task_update_requires_at_least_one_field(config: FeishuDataConfig, client: AsyncMock) -> None:
    tool = TaskUpdateTool(config, client)

    result = json.loads(await tool.execute(task_id="task_3"))

    assert "At least one update field" in result["error"]
    client.request.assert_not_called()


@pytest.mark.asyncio
async def test_task_update_rejects_unsupported_status(config: FeishuDataConfig, client: AsyncMock) -> None:
    tool = TaskUpdateTool(config, client)

    result = json.loads(await tool.execute(task_id="task_3", status="blocked"))

    assert "Unsupported status" in result["error"]
    client.request.assert_not_called()


@pytest.mark.asyncio
async def test_task_delete_path(config: FeishuDataConfig, client: AsyncMock) -> None:
    tool = TaskDeleteTool(config, client)
    client.request.return_value = {"code": 0}

    _ = await tool.execute(task_id="task_4")

    client.request.assert_called_once_with("DELETE", FeishuEndpoints.task_v2_task("task_4"))


@pytest.mark.asyncio
async def test_task_list_and_tasklist_list(config: FeishuDataConfig, client: AsyncMock) -> None:
    task_tool = TaskListTool(config, client)
    tasklist_tool = TasklistListTool(config, client)
    client.request.return_value = {"data": {"items": [], "has_more": False}}

    _ = await task_tool.execute(tasklist_id="list_1", page_size=10)
    _ = await tasklist_tool.execute(page_size=5)

    assert client.request.call_args_list[0].kwargs["params"] == {"tasklist_id": "list_1", "page_size": 10}
    assert client.request.call_args_list[1].kwargs["params"] == {"page_size": 5}


@pytest.mark.asyncio
async def test_subtask_create_and_comment_add(config: FeishuDataConfig, client: AsyncMock) -> None:
    subtask_tool = SubtaskCreateTool(config, client)
    comment_tool = TaskCommentAddTool(config, client)
    client.request.return_value = {"data": {}}

    _ = await subtask_tool.execute(task_id="task_parent", summary="child")
    _ = await comment_tool.execute(task_id="task_parent", content="LGTM")

    assert client.request.call_args_list[0].args[1] == FeishuEndpoints.task_v2_subtasks("task_parent")
    assert client.request.call_args_list[1].args[1] == FeishuEndpoints.task_v2_comments("task_parent")
