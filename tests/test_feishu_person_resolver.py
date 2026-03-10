import pytest

from nanobot.agent.tools.feishu_data.directory import BitableDirectorySearchTool
from nanobot.agent.tools.feishu_data.errors import FeishuDataAPIError
from nanobot.agent.tools.feishu_data.person_resolver import BitablePersonResolver, PersonResolutionAmbiguousError
from nanobot.config.schema import FeishuDataConfig


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def request(self, method, path, params=None, json_body=None, headers=None, **kwargs):
        self.calls.append({"method": method, "path": path, "params": params, "json_body": json_body, **kwargs})
        if isinstance(self.payload, list):
            current = self.payload.pop(0)
        else:
            current = self.payload
        if isinstance(current, Exception):
            raise current
        return current


class FakeUserTokenManager:
    def __init__(self, token: str = "user-token"):
        self.token = token
        self.calls: list[str] = []

    def get_valid_access_token(self, open_id: str) -> str:
        self.calls.append(open_id)
        return self.token


@pytest.mark.asyncio
async def test_person_resolver_resolves_name_and_caches_result():
    client = FakeClient(
        {
            "data": {
                "items": [
                    {"fields": {"姓名": "张三", "邮箱": "zhangsan@example.com", "OpenID": "ou_123"}}
                ]
            }
        }
    )
    resolver = BitablePersonResolver(
        FeishuDataConfig(enabled=True),
        client=client,
        directory={
            "app_token": "app-directory",
            "table_id": "tbl-directory",
            "lookup_fields": ["姓名", "邮箱"],
            "open_id_field": "OpenID",
        },
    )

    first = await resolver.resolve("张三")
    second = await resolver.resolve("张三")

    assert first == "ou_123"
    assert second == "ou_123"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_person_resolver_resolves_email_lookup():
    client = FakeClient(
        {
            "data": {
                "items": [
                    {"fields": {"姓名": "张三", "邮箱": "zhangsan@example.com", "OpenID": "ou_123"}}
                ]
            }
        }
    )
    resolver = BitablePersonResolver(
        FeishuDataConfig(enabled=True),
        client=client,
        directory={
            "app_token": "app-directory",
            "table_id": "tbl-directory",
            "lookup_fields": ["姓名", "邮箱"],
            "open_id_field": "OpenID",
        },
    )

    assert await resolver.resolve("zhangsan@example.com") == "ou_123"


@pytest.mark.asyncio
async def test_person_resolver_search_returns_directory_contacts():
    client = FakeClient(
        {
            "data": {
                "items": [
                    {"fields": {"姓名": "张三", "邮箱": "zhangsan@example.com", "OpenID": "ou_123"}},
                    {"fields": {"姓名": "李四", "邮箱": "lisi@example.com", "OpenID": "ou_456"}},
                ]
            }
        }
    )
    resolver = BitablePersonResolver(
        FeishuDataConfig(enabled=True),
        client=client,
        directory={
            "app_token": "app-directory",
            "table_id": "tbl-directory",
            "lookup_fields": ["姓名", "邮箱"],
            "open_id_field": "OpenID",
        },
    )

    contacts = await resolver.search(keyword="张", limit=5)

    assert contacts == [
        {
            "open_id": "ou_123",
            "display_name": "张三",
            "matched": {"姓名": "张三", "邮箱": "zhangsan@example.com"},
        }
    ]


@pytest.mark.asyncio
async def test_person_resolver_uses_app_contact_lookup_before_cache():
    client = FakeClient(
        {
            "data": {
                "items": [
                    {"name": "房怡康", "email": "fang@example.com", "mobile": "13800000000", "open_id": "ou_fang"}
                ]
            }
        }
    )
    resolver = BitablePersonResolver(FeishuDataConfig(enabled=True), client=client)

    first = await resolver.resolve("房怡康")
    second = await resolver.resolve("房怡康")

    assert first == "ou_fang"
    assert second == "ou_fang"
    assert len(client.calls) == 1
    assert client.calls[0]["auth_mode"] == "app"


@pytest.mark.asyncio
async def test_person_resolver_falls_back_to_user_oauth_when_app_lookup_fails():
    client = FakeClient(
        [
            FeishuDataAPIError(403, "permission denied"),
            {
                "data": {
                    "items": [
                        {"name": "房怡康", "email": "fang@example.com", "open_id": "ou_fang"}
                    ]
                }
            },
        ]
    )
    token_manager = FakeUserTokenManager("oauth-token")
    resolver = BitablePersonResolver(
        FeishuDataConfig(enabled=True),
        client=client,
        user_token_manager=token_manager,
    )

    resolved = await resolver.resolve("房怡康", actor_open_id="ou_actor")

    assert resolved == "ou_fang"
    assert token_manager.calls == ["ou_actor"]
    assert client.calls[1]["auth_mode"] == "user"
    assert client.calls[1]["bearer_token"] == "oauth-token"


@pytest.mark.asyncio
async def test_person_resolver_reports_ambiguous_matches():
    client = FakeClient(
        {
            "data": {
                "items": [
                    {"name": "张三", "open_id": "ou_1"},
                    {"name": "张三（法务）", "open_id": "ou_2"},
                ]
            }
        }
    )
    resolver = BitablePersonResolver(FeishuDataConfig(enabled=True), client=client)

    with pytest.raises(PersonResolutionAmbiguousError):
        await resolver.resolve("张三")


@pytest.mark.asyncio
async def test_bitable_directory_search_tool_works_without_workspace_directory_config():
    client = FakeClient(
        {
            "data": {
                "items": [
                    {"name": "房怡康", "email": "fang@example.com", "open_id": "ou_fang"},
                    {"name": "张三", "email": "zhangsan@example.com", "open_id": "ou_123"},
                ]
            }
        }
    )
    tool = BitableDirectorySearchTool(
        FeishuDataConfig(enabled=True),
        client=client,
    )

    payload = await tool.execute(keyword="房", limit=3)

    assert "房怡康" in payload
    assert "ou_fang" in payload


@pytest.mark.asyncio
async def test_bitable_directory_search_tool_uses_workspace_directory_config(tmp_path):
    workspace_rules = tmp_path / "feishu" / "bitable_rules.yaml"
    workspace_rules.parent.mkdir(parents=True, exist_ok=True)
    workspace_rules.write_text(
        """
version: 2
directory:
  app_token: app-directory
  table_id: tbl-directory
  lookup_fields:
    - 姓名
    - 邮箱
  open_id_field: OpenID
""".strip()
        + "\n",
        encoding="utf-8",
    )

    client = FakeClient(
        {
            "data": {
                "items": [
                    {"fields": {"姓名": "房怡康", "邮箱": "fang@example.com", "OpenID": "ou_fang"}},
                    {"fields": {"姓名": "张三", "邮箱": "zhangsan@example.com", "OpenID": "ou_123"}},
                ]
            }
        }
    )
    tool = BitableDirectorySearchTool(
        FeishuDataConfig(enabled=True),
        client=client,
        workspace=tmp_path,
    )

    payload = await tool.execute(keyword="房", limit=3)

    assert "房怡康" in payload
    assert "ou_fang" in payload
