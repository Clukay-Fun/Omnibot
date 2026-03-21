"""Feishu outbound delivery helpers."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.feishu.cards import FeishuCardPayload, build_feishu_card, render_feishu_card_fallback
from nanobot.feishu.client import FeishuClient
from nanobot.feishu.renderer import FeishuRenderer
from nanobot.utils.emoji import emojize_text


class FeishuOutboundMessenger:
    """Send rendered outbound messages through the Feishu client."""

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
    _AUDIO_EXTS = {".opus"}
    _VIDEO_EXTS = {".mp4", ".mov", ".avi"}

    def __init__(self, client_getter: Callable[[], Any | None]):
        self._client_getter = client_getter

    async def send(self, msg: OutboundMessage) -> bool:
        client = self._client_getter()
        if client is None:
            logger.warning("Feishu client not initialized")
            return False

        try:
            receive_id_type = FeishuClient.resolve_receive_id_type(msg.chat_id)
            loop = asyncio.get_running_loop()
            reply_to = self._reply_target(msg)
            success = True
            sent_any = False

            for file_path in msg.media:
                if not os.path.isfile(file_path):
                    logger.warning("Media file not found: {}", file_path)
                    success = False
                    continue
                media_ok = await self._send_media(loop, client, receive_id_type, msg.chat_id, file_path, reply_to=reply_to)
                sent_any = sent_any or media_ok
                success = success and media_ok

            if msg.feishu_card is not None:
                card_ok = await self._send_feishu_card(
                    loop,
                    client,
                    receive_id_type,
                    msg.chat_id,
                    msg.feishu_card,
                    reply_to=reply_to,
                )
                sent_any = sent_any or card_ok
                success = success and card_ok
            elif msg.content and msg.content.strip():
                content_ok = await self._send_content(
                    loop,
                    client,
                    receive_id_type,
                    msg.chat_id,
                    msg.content,
                    reply_to=reply_to,
                    delivery_mode=str((msg.metadata or {}).get("feishu_delivery") or ""),
                )
                sent_any = sent_any or content_ok
                success = success and content_ok
            return sent_any and success
        except Exception as exc:
            logger.error("Error sending Feishu message: {}", exc)
            return False

    async def _send_feishu_card(
        self,
        loop: asyncio.AbstractEventLoop,
        client: FeishuClient,
        receive_id_type: str,
        chat_id: str,
        card: FeishuCardPayload,
        *,
        reply_to: str | None = None,
    ) -> bool:
        try:
            payload = build_feishu_card(card)
        except Exception as exc:
            logger.warning("Failed to build Feishu Card 2.0 payload: {}", exc)
            return await self._send_feishu_card_fallback(
                loop, client, receive_id_type, chat_id, card, reply_to=reply_to
            )

        try:
            ok = await loop.run_in_executor(
                None,
                client.send_message_sync,
                receive_id_type,
                chat_id,
                "interactive",
                json.dumps(payload, ensure_ascii=False),
                reply_to,
            )
        except Exception as exc:
            logger.warning("Feishu Card 2.0 send failed: {}", exc)
            ok = False

        if ok:
            return True

        logger.warning(
            "Feishu Card 2.0 delivery failed for template '{}' to {}, falling back to post/text",
            card.template,
            chat_id,
        )
        return await self._send_feishu_card_fallback(
            loop, client, receive_id_type, chat_id, card, reply_to=reply_to
        )

    async def _send_feishu_card_fallback(
        self,
        loop: asyncio.AbstractEventLoop,
        client: FeishuClient,
        receive_id_type: str,
        chat_id: str,
        card: FeishuCardPayload,
        *,
        reply_to: str | None = None,
    ) -> bool:
        fallback_content = render_feishu_card_fallback(card)
        msg_type, payload = FeishuRenderer.render_reply_post(fallback_content)
        return bool(await loop.run_in_executor(
            None,
            client.send_message_sync,
            receive_id_type,
            chat_id,
            msg_type,
            payload,
            reply_to,
        ))

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
    ) -> bool:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in self._IMAGE_EXTS:
            key = await loop.run_in_executor(None, client.upload_image_sync, file_path)
            if key:
                return bool(await loop.run_in_executor(
                    None,
                    client.send_message_sync,
                    receive_id_type,
                    chat_id,
                    "image",
                    json.dumps({"image_key": key}, ensure_ascii=False),
                    reply_to,
                ))
            return False

        key = await loop.run_in_executor(None, client.upload_file_sync, file_path)
        if not key:
            return False
        msg_type = "media" if ext in self._AUDIO_EXTS or ext in self._VIDEO_EXTS else "file"
        return bool(await loop.run_in_executor(
            None,
            client.send_message_sync,
            receive_id_type,
            chat_id,
            msg_type,
            json.dumps({"file_key": key}, ensure_ascii=False),
            reply_to,
        ))

    async def _send_content(
        self,
        loop: asyncio.AbstractEventLoop,
        client: FeishuClient,
        receive_id_type: str,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        delivery_mode: str = "",
    ) -> bool:
        if delivery_mode == "turn_final":
            msg_type, payload = FeishuRenderer.render_final_reply(content)
            return bool(await loop.run_in_executor(
                None,
                client.send_message_sync,
                receive_id_type,
                chat_id,
                msg_type,
                payload,
                reply_to,
            ))

        if delivery_mode == "reply_post":
            msg_type, payload = FeishuRenderer.render_reply_post(content)
            return bool(await loop.run_in_executor(
                None,
                client.send_message_sync,
                receive_id_type,
                chat_id,
                msg_type,
                payload,
                reply_to,
            ))

        fmt = FeishuRenderer.detect_msg_format(content)
        if fmt == "text":
            return bool(await loop.run_in_executor(
                None,
                client.send_message_sync,
                receive_id_type,
                chat_id,
                "text",
                json.dumps({"text": emojize_text(content.strip())}, ensure_ascii=False),
                reply_to,
            ))

        if fmt == "post":
            return bool(await loop.run_in_executor(
                None,
                client.send_message_sync,
                receive_id_type,
                chat_id,
                "post",
                FeishuRenderer.markdown_to_post(content),
                reply_to,
            ))

        elements = FeishuRenderer.build_card_elements(content)
        success = True
        for chunk in FeishuRenderer.split_elements_by_table_limit(elements):
            card = {"config": {"wide_screen_mode": True}, "elements": chunk}
            ok = await loop.run_in_executor(
                None,
                client.send_message_sync,
                receive_id_type,
                chat_id,
                "interactive",
                json.dumps(card, ensure_ascii=False),
                reply_to,
            )
            success = success and bool(ok)
        return success
