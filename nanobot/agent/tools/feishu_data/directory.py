"""Directory read tools backed by the shared Feishu Bitable directory config."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.directory_config import load_directory_config
from nanobot.agent.tools.feishu_data.person_resolver import BitablePersonResolver
from nanobot.config.schema import FeishuDataConfig
from nanobot.oauth.feishu import FeishuUserTokenManager

if TYPE_CHECKING:
    from nanobot.agent.turn_runtime import TurnRuntime


class BitableDirectorySearchTool(Tool):
    def __init__(
        self,
        config: FeishuDataConfig,
        client: FeishuDataClient,
        *,
        workspace: Path | None = None,
        user_token_manager: FeishuUserTokenManager | None = None,
    ):
        self.config = config
        self.client = client
        self._workspace = workspace
        self._user_token_manager = user_token_manager
        self._runtime_channel = ""
        self._runtime_chat_id = ""
        self._runtime_sender_id = ""
        self._runtime_metadata: dict[str, Any] = {}

    def set_runtime_context(
        self,
        channel: str,
        chat_id: str,
        sender_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._runtime_channel = channel or ""
        self._runtime_chat_id = chat_id or ""
        self._runtime_sender_id = sender_id or ""
        self._runtime_metadata = dict(metadata or {})

    def set_turn_runtime(self, runtime: "TurnRuntime") -> None:
        self.set_runtime_context(runtime.channel, runtime.chat_id, runtime.sender_id, runtime.metadata)

    def _runtime_open_id(self) -> str:
        return (
            str(self._runtime_metadata.get("sender_open_id") or "").strip()
            or str(self._runtime_sender_id or "").strip()
        )

    @property
    def name(self) -> str:
        return "bitable_directory_search"

    @property
    def description(self) -> str:
        return (
            "Search or list Feishu contacts. "
            "Prefers contact APIs and can fall back to legacy directory bitable config when present."
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
        limit = max(1, int(kwargs.get("limit") or 10))
        keyword = str(kwargs.get("keyword") or "").strip() or None
        resolver = BitablePersonResolver(
            self.config,
            client=self.client,
            directory=directory,
            user_token_manager=self._user_token_manager,
        )
        contacts = await resolver.search(keyword=keyword, limit=limit, actor_open_id=self._runtime_open_id() or None)
        payload: dict[str, Any] = {
            "keyword": keyword or "",
            "contacts": contacts,
            "total": len(contacts),
            "source": "feishu_contacts",
        }
        app_token = str(directory.get("app_token") or "").strip()
        table_id = str(directory.get("table_id") or "").strip()
        lookup_fields = [str(item).strip() for item in directory.get("lookup_fields", []) if str(item).strip()]
        if app_token and table_id and lookup_fields:
            payload["directory"] = {
                "table_id": table_id,
                "lookup_fields": lookup_fields,
                "open_id_field": str(directory.get("open_id_field") or "open_id"),
            }
        return json.dumps(payload, ensure_ascii=False)
