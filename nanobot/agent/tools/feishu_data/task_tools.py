"""Feishu Task v2 tools."""

import json
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.endpoints import FeishuEndpoints
from nanobot.config.schema import FeishuDataConfig


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None}


class _TaskBaseTool(Tool):
    def __init__(self, config: FeishuDataConfig, client: FeishuDataClient):
        self.config = config
        self.client = client


class TaskCreateTool(_TaskBaseTool):
    @property
    def name(self) -> str:
        return "task_create"

    @property
    def description(self) -> str:
        return "Create a Task v2 task."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Task summary."},
                "description": {"type": "string", "description": "Task description."},
                "tasklist_id": {"type": "string", "description": "Target task list id."},
                "due": {"type": "string", "description": "Due datetime RFC3339."},
                "assignee_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary"],
        }

    async def execute(self, **kwargs: Any) -> str:
        body = _clean(
            {
                "summary": kwargs.get("summary"),
                "description": kwargs.get("description"),
                "tasklist_id": kwargs.get("tasklist_id"),
                "due": kwargs.get("due"),
                "assignee_ids": kwargs.get("assignee_ids"),
            }
        )
        if not body.get("summary"):
            return json.dumps({"error": "Missing summary."}, ensure_ascii=False)
        try:
            data = await self.client.request("POST", FeishuEndpoints.task_v2_tasks(), json_body=body)
            return json.dumps({"task": data.get("data", {})}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class TaskGetTool(_TaskBaseTool):
    @property
    def name(self) -> str:
        return "task_get"

    @property
    def description(self) -> str:
        return "Get a Task v2 task by id."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "Task ID."}},
            "required": ["task_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        task_id = str(kwargs.get("task_id") or "").strip()
        if not task_id:
            return json.dumps({"error": "Missing task_id."}, ensure_ascii=False)
        try:
            data = await self.client.request("GET", FeishuEndpoints.task_v2_task(task_id))
            return json.dumps({"task": data.get("data", {})}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class TaskUpdateTool(_TaskBaseTool):
    @property
    def name(self) -> str:
        return "task_update"

    @property
    def description(self) -> str:
        return "Update Task v2 fields."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID."},
                "summary": {"type": "string", "description": "Task summary."},
                "description": {"type": "string", "description": "Task description."},
                "status": {"type": "string", "description": "Task status."},
                "due": {"type": "string", "description": "Due datetime RFC3339."},
            },
            "required": ["task_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        task_id = str(kwargs.get("task_id") or "").strip()
        if not task_id:
            return json.dumps({"error": "Missing task_id."}, ensure_ascii=False)
        body = _clean(
            {
                "summary": kwargs.get("summary"),
                "description": kwargs.get("description"),
                "status": kwargs.get("status"),
                "due": kwargs.get("due"),
            }
        )
        try:
            data = await self.client.request("PATCH", FeishuEndpoints.task_v2_task(task_id), json_body=body)
            return json.dumps({"task": data.get("data", {})}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class TaskDeleteTool(_TaskBaseTool):
    @property
    def name(self) -> str:
        return "task_delete"

    @property
    def description(self) -> str:
        return "Delete a Task v2 task."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "Task ID."}},
            "required": ["task_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        task_id = str(kwargs.get("task_id") or "").strip()
        if not task_id:
            return json.dumps({"error": "Missing task_id."}, ensure_ascii=False)
        try:
            await self.client.request("DELETE", FeishuEndpoints.task_v2_task(task_id))
            return json.dumps({"success": True, "task_id": task_id}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e), "success": False}, ensure_ascii=False)


class TaskListTool(_TaskBaseTool):
    @property
    def name(self) -> str:
        return "task_list"

    @property
    def description(self) -> str:
        return "List Task v2 tasks."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "tasklist_id": {"type": "string"},
                "page_size": {"type": "integer"},
                "page_token": {"type": "string"},
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        params = _clean(
            {
                "tasklist_id": kwargs.get("tasklist_id"),
                "page_size": kwargs.get("page_size"),
                "page_token": kwargs.get("page_token"),
            }
        )
        try:
            data = await self.client.request("GET", FeishuEndpoints.task_v2_tasks(), params=params or None)
            payload = data.get("data", {})
            return json.dumps(
                {
                    "items": payload.get("items", []),
                    "has_more": payload.get("has_more", False),
                    "page_token": payload.get("page_token"),
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e), "items": []}, ensure_ascii=False)


class TasklistListTool(_TaskBaseTool):
    @property
    def name(self) -> str:
        return "tasklist_list"

    @property
    def description(self) -> str:
        return "List TaskList resources."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "page_size": {"type": "integer"},
                "page_token": {"type": "string"},
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        params = _clean({"page_size": kwargs.get("page_size"), "page_token": kwargs.get("page_token")})
        try:
            data = await self.client.request("GET", FeishuEndpoints.task_v2_tasklists(), params=params or None)
            payload = data.get("data", {})
            return json.dumps(
                {
                    "items": payload.get("items", []),
                    "has_more": payload.get("has_more", False),
                    "page_token": payload.get("page_token"),
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e), "items": []}, ensure_ascii=False)


class SubtaskCreateTool(_TaskBaseTool):
    @property
    def name(self) -> str:
        return "subtask_create"

    @property
    def description(self) -> str:
        return "Create a subtask under a parent task."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "summary": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["task_id", "summary"],
        }

    async def execute(self, **kwargs: Any) -> str:
        task_id = str(kwargs.get("task_id") or "").strip()
        summary = str(kwargs.get("summary") or "").strip()
        if not task_id:
            return json.dumps({"error": "Missing task_id."}, ensure_ascii=False)
        if not summary:
            return json.dumps({"error": "Missing summary."}, ensure_ascii=False)

        body = _clean({"summary": summary, "description": kwargs.get("description")})
        try:
            data = await self.client.request("POST", FeishuEndpoints.task_v2_subtasks(task_id), json_body=body)
            return json.dumps({"subtask": data.get("data", {})}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class TaskCommentAddTool(_TaskBaseTool):
    @property
    def name(self) -> str:
        return "task_comment_add"

    @property
    def description(self) -> str:
        return "Add a comment to a Task v2 task."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["task_id", "content"],
        }

    async def execute(self, **kwargs: Any) -> str:
        task_id = str(kwargs.get("task_id") or "").strip()
        content = str(kwargs.get("content") or "").strip()
        if not task_id:
            return json.dumps({"error": "Missing task_id."}, ensure_ascii=False)
        if not content:
            return json.dumps({"error": "Missing content."}, ensure_ascii=False)

        try:
            data = await self.client.request(
                "POST",
                FeishuEndpoints.task_v2_comments(task_id),
                json_body={"content": content},
            )
            return json.dumps({"comment": data.get("data", {})}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
