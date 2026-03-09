"""Feishu IM message history tool with user OAuth support."""

import json
from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.endpoints import FeishuEndpoints
from nanobot.config.schema import FeishuDataConfig
from nanobot.oauth.feishu import FeishuReauthorizationRequired, FeishuUserTokenManager

if TYPE_CHECKING:
    from nanobot.agent.turn_runtime import TurnRuntime


class MessageHistoryListTool(Tool):
    def __init__(
        self,
        config: FeishuDataConfig,
        client: FeishuDataClient,
        *,
        user_token_manager: FeishuUserTokenManager | None = None,
    ):
        self.config = config
        self.client = client
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

    @property
    def name(self) -> str:
        return "message_history_list"

    @property
    def description(self) -> str:
        return "List message history from a Feishu chat (prefers user OAuth token)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Target chat_id. Defaults to current context."},
                "container_id_type": {"type": "string", "description": "chat or thread. Defaults to chat."},
                "page_size": {"type": "integer", "description": "Page size."},
                "page_token": {"type": "string", "description": "Pagination token."},
                "start_time": {"type": "string", "description": "Unix timestamp in seconds."},
                "end_time": {"type": "string", "description": "Unix timestamp in seconds."},
                "sort_type": {"type": "string", "description": "ByCreateTimeAsc or ByCreateTimeDesc."},
                "auth_mode": {"type": "string", "enum": ["user", "app"], "description": "Auth mode."},
            },
        }

    def _runtime_open_id(self) -> str:
        candidate = (
            str(self._runtime_metadata.get("sender_open_id") or "").strip()
            or str(self._runtime_sender_id or "").strip()
        )
        return candidate

    @staticmethod
    def _connect_hint(reason: str) -> str:
        return (
            f"{reason} 请先发送 /connect 完成飞书用户授权，然后重试 message_history_list。"
        )

    async def execute(self, **kwargs: Any) -> str:
        auth_mode = str(kwargs.get("auth_mode") or "user").strip().lower() or "user"
        chat_id = str(kwargs.get("chat_id") or self._runtime_chat_id or "").strip()
        if not chat_id:
            return json.dumps({"error": "Missing chat_id."}, ensure_ascii=False)

        params: dict[str, Any] = {
            "container_id_type": kwargs.get("container_id_type") or "chat",
            "container_id": chat_id,
        }
        for key in ("page_size", "page_token", "start_time", "end_time", "sort_type"):
            val = kwargs.get(key)
            if val is not None:
                params[key] = val

        request_kwargs: dict[str, Any] = {"auth_mode": auth_mode}

        if auth_mode == "user":
            open_id = self._runtime_open_id()
            if not open_id:
                return json.dumps(
                    {"error": self._connect_hint("当前会话缺少 sender open_id。"), "needs_connect": True},
                    ensure_ascii=False,
                )
            if self._user_token_manager is None:
                return json.dumps(
                    {"error": self._connect_hint("OAuth 服务未启用。"), "needs_connect": True},
                    ensure_ascii=False,
                )
            try:
                token = self._user_token_manager.get_valid_access_token(open_id)
                request_kwargs["bearer_token"] = token
            except FeishuReauthorizationRequired:
                return json.dumps(
                    {"error": self._connect_hint("授权已失效或尚未授权。"), "needs_connect": True},
                    ensure_ascii=False,
                )
            except Exception as e:
                return json.dumps({"error": str(e), "needs_connect": True}, ensure_ascii=False)

        try:
            data = await self.client.request(
                "GET",
                FeishuEndpoints.im_message_list(),
                params=params,
                **request_kwargs,
            )
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
