"""Feishu Calendar toolset (v4)."""

import json
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.endpoints import FeishuEndpoints
from nanobot.config.schema import FeishuDataConfig


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None}


class _CalendarBaseTool(Tool):
    def __init__(self, config: FeishuDataConfig, client: FeishuDataClient):
        self.config = config
        self.client = client


class CalendarListTool(_CalendarBaseTool):
    @property
    def name(self) -> str:
        return "calendar_list"

    @property
    def description(self) -> str:
        return "List Feishu calendars available to current identity."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "page_size": {"type": "integer", "description": "Page size."},
                "page_token": {"type": "string", "description": "Pagination token."},
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        params = _drop_none({"page_size": kwargs.get("page_size"), "page_token": kwargs.get("page_token")})
        try:
            data = await self.client.request("GET", FeishuEndpoints.calendar_list(), params=params or None)
            payload = data.get("data", {})
            return json.dumps({
                "items": payload.get("items", []),
                "has_more": payload.get("has_more", False),
                "page_token": payload.get("page_token"),
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e), "items": []}, ensure_ascii=False)


class CalendarCreateTool(_CalendarBaseTool):
    @property
    def name(self) -> str:
        return "calendar_create"

    @property
    def description(self) -> str:
        return "Create a Feishu calendar."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Calendar title."},
                "description": {"type": "string", "description": "Calendar description."},
                "permissions": {"type": "string", "description": "Calendar permission level."},
                "color": {"type": "integer", "description": "Calendar color index."},
            },
            "required": ["summary"],
        }

    async def execute(self, **kwargs: Any) -> str:
        body = _drop_none(
            {
                "summary": kwargs.get("summary"),
                "description": kwargs.get("description"),
                "permissions": kwargs.get("permissions"),
                "color": kwargs.get("color"),
            }
        )
        if not body.get("summary"):
            return json.dumps({"error": "Missing summary."}, ensure_ascii=False)
        try:
            data = await self.client.request("POST", FeishuEndpoints.calendar_list(), json_body=body)
            return json.dumps({"calendar": data.get("data", {})}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class CalendarUpdateTool(_CalendarBaseTool):
    @property
    def name(self) -> str:
        return "calendar_update"

    @property
    def description(self) -> str:
        return "Update Feishu calendar metadata."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string", "description": "Calendar ID."},
                "summary": {"type": "string", "description": "New title."},
                "description": {"type": "string", "description": "New description."},
                "permissions": {"type": "string", "description": "Permission level."},
                "color": {"type": "integer", "description": "Color index."},
            },
            "required": ["calendar_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        calendar_id = str(kwargs.get("calendar_id") or "").strip()
        if not calendar_id:
            return json.dumps({"error": "Missing calendar_id."}, ensure_ascii=False)
        body = _drop_none(
            {
                "summary": kwargs.get("summary"),
                "description": kwargs.get("description"),
                "permissions": kwargs.get("permissions"),
                "color": kwargs.get("color"),
            }
        )
        try:
            data = await self.client.request("PATCH", FeishuEndpoints.calendar_detail(calendar_id), json_body=body)
            return json.dumps({"calendar": data.get("data", {})}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class CalendarDeleteTool(_CalendarBaseTool):
    @property
    def name(self) -> str:
        return "calendar_delete"

    @property
    def description(self) -> str:
        return "Delete a Feishu calendar."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string", "description": "Calendar ID."},
            },
            "required": ["calendar_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        calendar_id = str(kwargs.get("calendar_id") or "").strip()
        if not calendar_id:
            return json.dumps({"error": "Missing calendar_id."}, ensure_ascii=False)
        try:
            await self.client.request("DELETE", FeishuEndpoints.calendar_detail(calendar_id))
            return json.dumps({"success": True, "calendar_id": calendar_id}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e), "success": False}, ensure_ascii=False)


class CalendarFreebusyTool(_CalendarBaseTool):
    @property
    def name(self) -> str:
        return "calendar_freebusy"

    @property
    def description(self) -> str:
        return "Query free/busy windows for calendars."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "time_min": {"type": "string", "description": "RFC3339 start time."},
                "time_max": {"type": "string", "description": "RFC3339 end time."},
                "calendar_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Target calendar IDs.",
                },
            },
            "required": ["time_min", "time_max", "calendar_ids"],
        }

    async def execute(self, **kwargs: Any) -> str:
        calendar_ids = kwargs.get("calendar_ids") if isinstance(kwargs.get("calendar_ids"), list) else []
        if not calendar_ids:
            return json.dumps({"error": "Missing calendar_ids."}, ensure_ascii=False)
        body = {
            "time_min": kwargs.get("time_min"),
            "time_max": kwargs.get("time_max"),
            "calendars": [{"calendar_id": cid} for cid in calendar_ids if isinstance(cid, str) and cid],
        }
        if not body.get("time_min") or not body.get("time_max"):
            return json.dumps({"error": "Missing time_min or time_max."}, ensure_ascii=False)
        try:
            data = await self.client.request("POST", FeishuEndpoints.calendar_freebusy(), json_body=body)
            return json.dumps({"freebusy": data.get("data", {})}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
