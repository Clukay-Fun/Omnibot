"""Feishu inbound media loading helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.config.paths import get_media_dir
from nanobot.feishu.client import FeishuClient
from nanobot.feishu.types import TranslatedFeishuMessage


class FeishuInboundMediaLoader:
    """Download Feishu attachments and enrich translated inbound messages."""

    def __init__(
        self,
        client_getter: Callable[[], FeishuClient | None],
        *,
        groq_api_key: str,
        react_emoji: str,
    ):
        self._client_getter = client_getter
        self.groq_api_key = groq_api_key
        self.react_emoji = react_emoji

    async def load_translated_media(
        self,
        _envelope: Any,
        translated: TranslatedFeishuMessage,
    ) -> None:
        msg_type = translated.metadata.get("msg_type")
        content_json = translated.metadata.get("content_json") or {}
        message_id = translated.metadata.get("message_id")
        content_parts = [translated.content] if translated.content else []

        if message_id:
            await self._add_reaction(str(message_id), self.react_emoji)

        if msg_type == "post":
            for img_key in translated.metadata.get("post_image_keys", []):
                file_path, content_text = await self._download_and_save_media("image", {"image_key": img_key}, message_id)
                if file_path:
                    translated.media.append(file_path)
                content_parts.append(content_text)
            translated.content = "\n".join(part for part in content_parts if part)
            return

        if msg_type not in {"image", "audio", "file", "media"}:
            translated.content = "\n".join(part for part in content_parts if part)
            return

        file_path, content_text = await self._download_and_save_media(str(msg_type), content_json, message_id)
        if file_path:
            translated.media.append(file_path)

        if msg_type == "audio" and file_path and self.groq_api_key:
            content_text = await self._transcribe_audio(file_path, content_text)

        translated.content = content_text

    async def _add_reaction(self, message_id: str, emoji_type: str) -> None:
        client = self._client_getter()
        if client is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, client.add_reaction_sync, message_id, emoji_type)

    async def _download_and_save_media(
        self,
        msg_type: str,
        content_json: dict[str, Any],
        message_id: str | None = None,
    ) -> tuple[str | None, str]:
        client = self._client_getter()
        if client is None:
            return None, f"[{msg_type}: download failed]"

        loop = asyncio.get_running_loop()
        media_dir = get_media_dir("feishu")
        data, filename = None, None

        if msg_type == "image":
            image_key = content_json.get("image_key")
            if image_key and message_id:
                data, filename = await loop.run_in_executor(None, client.download_image_sync, message_id, image_key)
                if not filename:
                    filename = f"{str(image_key)[:16]}.jpg"
        elif msg_type in {"audio", "file", "media"}:
            file_key = content_json.get("file_key")
            if file_key and message_id:
                data, filename = await loop.run_in_executor(None, client.download_file_sync, message_id, file_key, msg_type)
                if not filename:
                    filename = str(file_key)[:16]
                if msg_type == "audio" and not str(filename).endswith(".opus"):
                    filename = f"{filename}.opus"

        if data and filename:
            file_path = Path(media_dir) / str(filename)
            file_path.write_bytes(data)
            logger.debug("Downloaded {} to {}", msg_type, file_path)
            return str(file_path), f"[{msg_type}: {filename}]"

        return None, f"[{msg_type}: download failed]"

    async def _transcribe_audio(self, file_path: str, fallback_text: str) -> str:
        try:
            from nanobot.providers.transcription import GroqTranscriptionProvider

            transcriber = GroqTranscriptionProvider(api_key=self.groq_api_key)
            transcription = await transcriber.transcribe(file_path)
            if transcription:
                return f"[transcription: {transcription}]"
        except Exception as exc:
            logger.warning("Failed to transcribe audio: {}", exc)
        return fallback_text
