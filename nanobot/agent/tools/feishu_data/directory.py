"""Directory read tools backed by the shared Feishu Bitable directory config."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.directory_config import load_directory_config
from nanobot.agent.tools.feishu_data.person_resolver import BitablePersonResolver
from nanobot.config.schema import FeishuDataConfig


class BitableDirectorySearchTool(Tool):
    def __init__(self, config: FeishuDataConfig, client: FeishuDataClient, *, workspace: Path | None = None):
        self.config = config
        self.client = client
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "bitable_directory_search"

    @property
    def description(self) -> str:
        return (
            "Search or list people from the configured Feishu directory bitable. "
            "Use this when the user asks who is in the directory/contacts, or asks to find a colleague/open_id."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Optional name/email keyword. Omit it to list directory contacts.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of contacts to return.",
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        directory = load_directory_config(self._workspace)
        app_token = str(directory.get("app_token") or "").strip()
        table_id = str(directory.get("table_id") or "").strip()
        lookup_fields = [str(item).strip() for item in directory.get("lookup_fields", []) if str(item).strip()]
        if not app_token or not table_id or not lookup_fields:
            return json.dumps(
                {
                    "error": "Directory config is missing. Please fill workspace/feishu/bitable_rules.yaml -> directory first.",
                    "contacts": [],
                },
                ensure_ascii=False,
            )

        limit = max(1, int(kwargs.get("limit") or 10))
        keyword = str(kwargs.get("keyword") or "").strip() or None
        resolver = BitablePersonResolver(self.config, client=self.client, directory=directory)
        contacts = await resolver.search(keyword=keyword, limit=limit)
        return json.dumps(
            {
                "keyword": keyword or "",
                "contacts": contacts,
                "total": len(contacts),
                "directory": {
                    "table_id": table_id,
                    "lookup_fields": lookup_fields,
                    "open_id_field": str(directory.get("open_id_field") or "open_id"),
                },
            },
            ensure_ascii=False,
        )
