"""Translate Feishu inbound payloads into normalized channel messages."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

from nanobot.config.schema import FeishuConfig
from nanobot.feishu.payload import read_path
from nanobot.feishu.parser import MSG_TYPE_MAP, _extract_post_content, _extract_share_card_content
from nanobot.feishu.types import TranslatedFeishuMessage


class FeishuAdapter:
    """Dumb adapter: protocol translation only, no business decisions."""

    _MENTION_TAG_RE = re.compile(r"<at[^>]*>.*?</at>", re.IGNORECASE)
    _MENTION_PLACEHOLDER_RE = re.compile(r"@_[^\s]+")

    def __init__(
        self,
        config: FeishuConfig,
        ttl_manager: Any | None = None,
        overflow_manager: Any | None = None,
        overflow_keep_messages: int = 0,
    ):
        self.config = config
        self.ttl_manager = ttl_manager
        self.overflow_manager = overflow_manager
        self.overflow_keep_messages = overflow_keep_messages

    async def translate_message(self, payload: Any) -> TranslatedFeishuMessage | None:
        event = read_path(payload, "event") or payload
        message = read_path(event, "message")
        sender = read_path(event, "sender")
        if message is None or sender is None:
            return None

        sender_type = read_path(sender, "sender_type")
        if sender_type == "bot":
            return None

        sender_id = read_path(sender, "sender_id", "open_id") or "unknown"
        chat_id = read_path(message, "chat_id") or ""
        chat_type = read_path(message, "chat_type") or "p2p"
        msg_type = read_path(message, "message_type") or "text"
        message_id = read_path(message, "message_id")
        tenant_key = read_path(payload, "header", "tenant_key")
        session_key = self._build_session_key(chat_type, chat_id, sender_id)

        if self.ttl_manager is not None:
            await self.ttl_manager.maybe_expire(session_key, str(tenant_key or ""), sender_id)
        if self.overflow_manager is not None and self.overflow_keep_messages > 0:
            await self.overflow_manager.maybe_enqueue_overflow(
                session_key,
                str(tenant_key or ""),
                sender_id,
                keep_messages=self.overflow_keep_messages,
                start_worker=False,
            )

        content_json = self._parse_content_json(read_path(message, "content"))
        content = ""
        image_keys: list[str] = []

        if msg_type == "text":
            content = content_json.get("text", "")
        elif msg_type == "post":
            content, image_keys = _extract_post_content(content_json)
        elif msg_type in (
            "share_chat",
            "share_user",
            "interactive",
            "share_calendar_event",
            "system",
            "merge_forward",
        ):
            content = _extract_share_card_content(content_json, msg_type)
        else:
            content = MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")

        content = self._strip_mentions(content)
        reply_to = chat_id if chat_type == "group" else sender_id

        metadata = {
            "message_id": message_id,
            "chat_type": chat_type,
            "msg_type": msg_type,
            "tenant_key": tenant_key,
            "raw_chat_id": chat_id,
            "user_open_id": sender_id,
            "content_json": content_json,
            "turn_id": f"feishu-turn-{uuid4().hex}",
            "stream_id": f"feishu-stream-{uuid4().hex}",
        }
        if image_keys:
            metadata["post_image_keys"] = image_keys

        return TranslatedFeishuMessage(
            sender_id=sender_id,
            chat_id=reply_to,
            content=content,
            metadata=metadata,
            session_key=session_key,
        )

    def _build_session_key(self, chat_type: str, chat_id: str, user_open_id: str) -> str:
        if chat_type != "group":
            return f"feishu:dm:{user_open_id}"
        if self.config.group_session_mode == "shared":
            return f"feishu:chat:{chat_id}"
        return f"feishu:chat:{chat_id}:user:{user_open_id}"

    def _strip_mentions(self, content: str) -> str:
        stripped = self._MENTION_TAG_RE.sub(" ", content)
        stripped = self._MENTION_PLACEHOLDER_RE.sub(" ", stripped)
        return " ".join(stripped.split())

    @staticmethod
    def _parse_content_json(content: Any) -> dict[str, Any]:
        if isinstance(content, dict):
            return content
        if not isinstance(content, str) or not content:
            return {}
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
