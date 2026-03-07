import json
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.endpoints import FeishuEndpoints
from nanobot.agent.tools.feishu_data.message_history import MessageHistoryListTool
from nanobot.config.schema import FeishuDataConfig
from nanobot.oauth.feishu import FeishuReauthorizationRequired


class FakeUserTokenManager:
    def __init__(self, token: str | None = None, raise_reauth: bool = False):
        self.token = token
        self.raise_reauth = raise_reauth

    def get_valid_access_token(self, open_id: str) -> str:
        if self.raise_reauth:
            raise FeishuReauthorizationRequired("reauth")
        assert open_id
        assert self.token is not None
        return self.token


@pytest.fixture
def config() -> FeishuDataConfig:
    return FeishuDataConfig(enabled=True, app_id="id", app_secret="secret")


@pytest.fixture
def client() -> AsyncMock:
    return AsyncMock(spec=FeishuDataClient)


@pytest.mark.asyncio
async def test_message_history_uses_user_token_with_runtime_sender(config: FeishuDataConfig, client: AsyncMock) -> None:
    tool = MessageHistoryListTool(config, client, user_token_manager=FakeUserTokenManager(token="user-token"))
    tool.set_runtime_context("feishu", "oc_123", "ou_456", {})
    client.request.return_value = {"data": {"items": [{"message_id": "om_1"}], "has_more": False}}

    result = json.loads(await tool.execute(page_size=20))

    assert result["items"][0]["message_id"] == "om_1"
    client.request.assert_called_once_with(
        "GET",
        FeishuEndpoints.im_message_list(),
        params={"container_id_type": "chat", "container_id": "oc_123", "page_size": 20},
        auth_mode="user",
        bearer_token="user-token",
    )


@pytest.mark.asyncio
async def test_message_history_unauthed_returns_connect_hint(config: FeishuDataConfig, client: AsyncMock) -> None:
    tool = MessageHistoryListTool(config, client, user_token_manager=FakeUserTokenManager(raise_reauth=True))
    tool.set_runtime_context("feishu", "oc_123", "ou_456", {})

    result = json.loads(await tool.execute())

    assert result["needs_connect"] is True
    assert "/connect" in result["error"]
    client.request.assert_not_called()


@pytest.mark.asyncio
async def test_message_history_missing_sender_returns_connect_hint(config: FeishuDataConfig, client: AsyncMock) -> None:
    tool = MessageHistoryListTool(config, client, user_token_manager=FakeUserTokenManager(token="x"))
    tool.set_runtime_context("feishu", "oc_123", "", {})

    result = json.loads(await tool.execute())

    assert result["needs_connect"] is True
    assert "sender open_id" in result["error"]


@pytest.mark.asyncio
async def test_message_history_app_auth_mode_without_oauth(config: FeishuDataConfig, client: AsyncMock) -> None:
    tool = MessageHistoryListTool(config, client, user_token_manager=None)
    tool.set_runtime_context("feishu", "oc_123", "", {})
    client.request.return_value = {"data": {"items": [], "has_more": False}}

    _ = await tool.execute(auth_mode="app")

    client.request.assert_called_once_with(
        "GET",
        FeishuEndpoints.im_message_list(),
        params={"container_id_type": "chat", "container_id": "oc_123"},
        auth_mode="app",
    )
