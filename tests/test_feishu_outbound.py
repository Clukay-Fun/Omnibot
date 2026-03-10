from __future__ import annotations

import json
from typing import cast

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.feishu.outbound import FeishuOutboundMessenger


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, dict, str | None]] = []
        self.uploaded_images: list[str] = []
        self.uploaded_files: list[str] = []

    @staticmethod
    def resolve_receive_id_type(receive_id: str) -> str:
        return "chat_id" if receive_id.startswith("oc_") else "open_id"

    def upload_image_sync(self, file_path: str) -> str:
        self.uploaded_images.append(file_path)
        return "img_key"

    def upload_file_sync(self, file_path: str) -> str:
        self.uploaded_files.append(file_path)
        return "file_key"

    def send_message_sync(
        self,
        receive_id_type: str,
        receive_id: str,
        msg_type: str,
        content: str,
        reply_to: str | None = None,
    ) -> bool:
        self.sent.append((receive_id_type, receive_id, msg_type, json.loads(content), reply_to))
        return True


@pytest.mark.asyncio
async def test_outbound_messenger_sends_text_message() -> None:
    client = _FakeClient()
    messenger = FeishuOutboundMessenger(lambda: cast(object, client))

    await messenger.send(OutboundMessage(channel="feishu", chat_id="ou_123", content="hello", reply_to="om_source_5"))

    assert client.sent == [("open_id", "ou_123", "text", {"text": "hello"}, "om_source_5")]


@pytest.mark.asyncio
async def test_outbound_messenger_sends_image_attachments(tmp_path) -> None:
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"png")
    client = _FakeClient()
    messenger = FeishuOutboundMessenger(lambda: cast(object, client))

    await messenger.send(OutboundMessage(channel="feishu", chat_id="oc_chat_1", content="", media=[str(image_path)], reply_to="om_source_6"))

    assert client.uploaded_images == [str(image_path)]
    assert client.sent == [("chat_id", "oc_chat_1", "image", {"image_key": "img_key"}, "om_source_6")]
