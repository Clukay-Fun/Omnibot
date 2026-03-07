"""Feishu Bitable admin tools (app/table/view creation)."""

import json
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.endpoints import FeishuEndpoints
from nanobot.config.schema import FeishuDataConfig


class _BitableAdminBaseTool(Tool):
    def __init__(self, config: FeishuDataConfig, client: FeishuDataClient):
        self.config = config
        self.client = client


class BitableAppCreateTool(_BitableAdminBaseTool):
    @property
    def name(self) -> str:
        return "bitable_app_create"

    @property
    def description(self) -> str:
        return "Create a Feishu Bitable app."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Bitable app name."},
                "folder_token": {"type": "string", "description": "Target folder token."},
                "time_zone": {"type": "string", "description": "Timezone for app."},
            },
            "required": ["name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        name = str(kwargs.get("name") or "").strip()
        if not name:
            return json.dumps({"error": "Missing name."}, ensure_ascii=False)
        body = {"name": name}
        if kwargs.get("folder_token"):
            body["folder_token"] = kwargs.get("folder_token")
        if kwargs.get("time_zone"):
            body["time_zone"] = kwargs.get("time_zone")
        try:
            data = await self.client.request("POST", FeishuEndpoints.bitable_apps(), json_body=body)
            return json.dumps({"app": data.get("data", {})}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class BitableTableCreateTool(_BitableAdminBaseTool):
    @property
    def name(self) -> str:
        return "bitable_table_create"

    @property
    def description(self) -> str:
        return "Create a table inside a Bitable app."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app_token": {"type": "string", "description": "Bitable app token."},
                "name": {"type": "string", "description": "Table name."},
                "default_view_name": {"type": "string", "description": "Optional default view name."},
            },
            "required": ["app_token", "name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        app_token = str(kwargs.get("app_token") or "").strip()
        name = str(kwargs.get("name") or "").strip()
        if not app_token:
            return json.dumps({"error": "Missing app_token."}, ensure_ascii=False)
        if not name:
            return json.dumps({"error": "Missing name."}, ensure_ascii=False)

        table_obj: dict[str, Any] = {"name": name}
        if kwargs.get("default_view_name"):
            table_obj["default_view_name"] = kwargs.get("default_view_name")
        try:
            data = await self.client.request(
                "POST",
                FeishuEndpoints.bitable_tables(app_token),
                json_body={"table": table_obj},
            )
            return json.dumps({"table": data.get("data", {})}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class BitableViewCreateTool(_BitableAdminBaseTool):
    @property
    def name(self) -> str:
        return "bitable_view_create"

    @property
    def description(self) -> str:
        return "Create a view for a Bitable table."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app_token": {"type": "string", "description": "Bitable app token."},
                "table_id": {"type": "string", "description": "Table id."},
                "view_name": {"type": "string", "description": "View name."},
                "view_type": {"type": "string", "description": "grid/gantt/kanban/gallery/form."},
            },
            "required": ["app_token", "table_id", "view_name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        app_token = str(kwargs.get("app_token") or "").strip()
        table_id = str(kwargs.get("table_id") or "").strip()
        view_name = str(kwargs.get("view_name") or "").strip()
        if not app_token:
            return json.dumps({"error": "Missing app_token."}, ensure_ascii=False)
        if not table_id:
            return json.dumps({"error": "Missing table_id."}, ensure_ascii=False)
        if not view_name:
            return json.dumps({"error": "Missing view_name."}, ensure_ascii=False)

        body: dict[str, Any] = {"view_name": view_name}
        if kwargs.get("view_type"):
            body["view_type"] = kwargs.get("view_type")
        try:
            data = await self.client.request(
                "POST",
                FeishuEndpoints.bitable_views(app_token, table_id),
                json_body=body,
            )
            return json.dumps({"view": data.get("data", {})}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
