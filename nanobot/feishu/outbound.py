"""Feishu outbound delivery helpers."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.feishu.client import FeishuClient
from nanobot.feishu.renderer import FeishuRenderer


class FeishuOutboundMessenger:
    """Send rendered outbound messages through the Feishu client."""

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
    _AUDIO_EXTS = {".opus"}
    _VIDEO_EXTS = {".mp4", ".mov", ".avi"}

    def __init__(self, client_getter: Callable[[], Any | None]):
        self._client_getter = client_getter

    async def send(self, msg: OutboundMessage) -> None:
        client = self._client_getter()
        if client is None:
            logger.warning("Feishu client not initialized")
            return

        try:
            receive_id_type = FeishuClient.resolve_receive_id_type(msg.chat_id)
            loop = asyncio.get_running_loop()
            reply_to = self._reply_target(msg)

            for file_path in msg.media:
                if not os.path.isfile(file_path):
                    logger.warning("Media file not found: {}", file_path)
                    continue
                await self._send_media(loop, client, receive_id_type, msg.chat_id, file_path, reply_to=reply_to)

            if msg.content and msg.content.strip():
                await self._send_content(loop, client, receive_id_type, msg.chat_id, msg.content, reply_to=reply_to)
        except Exception as exc:
            logger.error("Error sending Feishu message: {}", exc)

    @staticmethod
    def _reply_target(msg: OutboundMessage) -> str | None:
        if msg.reply_to:
            return msg.reply_to
        metadata = msg.metadata or {}
        reply_to = metadata.get("message_id")
        return str(reply_to) if reply_to else None

    async def _send_media(
        self,
        loop: asyncio.AbstractEventLoop,
        client: FeishuClient,
        receive_id_type: str,
        chat_id: str,
        file_path: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in self._IMAGE_EXTS:
            key = await loop.run_in_executor(None, client.upload_image_sync, file_path)
            if key:
                await loop.run_in_executor(
                    None,
                    client.send_message_sync,
                    receive_id_type,
                    chat_id,
                    "image",
                    json.dumps({"image_key": key}, ensure_ascii=False),
                    reply_to,
                )
            return

        key = await loop.run_in_executor(None, client.upload_file_sync, file_path)
        if not key:
            return
        msg_type = "media" if ext in self._AUDIO_EXTS or ext in self._VIDEO_EXTS else "file"
        await loop.run_in_executor(
            None,
            client.send_message_sync,
            receive_id_type,
            chat_id,
            msg_type,
            json.dumps({"file_key": key}, ensure_ascii=False),
            reply_to,
        )

    async def _send_content(
        self,
        loop: asyncio.AbstractEventLoop,
        client: FeishuClient,
        receive_id_type: str,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        fmt = FeishuRenderer.detect_msg_format(content)
        if fmt == "text":
            await loop.run_in_executor(
                None,
                client.send_message_sync,
                receive_id_type,
                chat_id,
                "text",
                json.dumps({"text": content.strip()}, ensure_ascii=False),
                reply_to,
            )
            return

        if fmt == "post":
            await loop.run_in_executor(
                None,
                client.send_message_sync,
                receive_id_type,
                chat_id,
                "post",
                FeishuRenderer.markdown_to_post(content),
                reply_to,
            )
            return

        elements = FeishuRenderer.build_card_elements(content)
        for chunk in FeishuRenderer.split_elements_by_table_limit(elements):
            card = {"config": {"wide_screen_mode": True}, "elements": chunk}
            await loop.run_in_executor(
                None,
                client.send_message_sync,
                receive_id_type,
                chat_id,
                "interactive",
                json.dumps(card, ensure_ascii=False),
                reply_to,
            )
