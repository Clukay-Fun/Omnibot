from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nanobot.feishu.media import FeishuInboundMediaLoader
from nanobot.feishu.types import TranslatedFeishuMessage


class _FakeClient:
    def download_image_sync(self, message_id: str, image_key: str):
        return b"image-bytes", f"{image_key}.jpg"

    def download_file_sync(self, message_id: str, file_key: str, resource_type: str = "file"):
        return b"file-bytes", f"{file_key}.bin"


@pytest.mark.asyncio
async def test_media_loader_downloads_post_images(tmp_path: Path) -> None:
    client = _FakeClient()
    loader = FeishuInboundMediaLoader(lambda: client, groq_api_key="")
    translated = TranslatedFeishuMessage(
        sender_id="ou_user_1",
        chat_id="ou_user_1",
        content="hello",
        metadata={
            "message_id": "om_1",
            "msg_type": "post",
            "content_json": {},
            "post_image_keys": ["img_1"],
        },
        session_key="feishu:dm:ou_user_1",
    )

    with patch("nanobot.feishu.media.get_media_dir", return_value=tmp_path):
        await loader.load_translated_media(None, translated)

    assert len(translated.media) == 1
    assert translated.content.endswith("[image: img_1.jpg]")


@pytest.mark.asyncio
async def test_media_loader_downloads_file_message(tmp_path: Path) -> None:
    client = _FakeClient()
    loader = FeishuInboundMediaLoader(lambda: client, groq_api_key="")
    translated = TranslatedFeishuMessage(
        sender_id="ou_user_1",
        chat_id="ou_user_1",
        content="",
        metadata={
            "message_id": "om_2",
            "msg_type": "file",
            "content_json": {"file_key": "file_1"},
        },
        session_key="feishu:dm:ou_user_1",
    )

    with patch("nanobot.feishu.media.get_media_dir", return_value=tmp_path):
        await loader.load_translated_media(None, translated)

    assert len(translated.media) == 1
    assert translated.content == "[file: file_1.bin]"
