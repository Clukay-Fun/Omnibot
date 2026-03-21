from __future__ import annotations

import json
from typing import cast

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.feishu.cards import FeishuCardPayload
from nanobot.feishu.outbound import FeishuOutboundMessenger


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, dict, str | None]] = []
        self.uploaded_images: list[str] = []
        self.uploaded_files: list[str] = []
        self.fail_send = False
        self.fail_interactive_sends = 0

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
        if msg_type == "interactive" and self.fail_interactive_sends > 0:
            self.fail_interactive_sends -= 1
            return False
        return not self.fail_send


@pytest.mark.asyncio
async def test_outbound_messenger_sends_text_message() -> None:
    client = _FakeClient()
    messenger = FeishuOutboundMessenger(lambda: cast(object, client))

    ok = await messenger.send(OutboundMessage(channel="feishu", chat_id="ou_123", content="hello", reply_to="om_source_5"))

    assert ok is True
    assert client.sent == [("open_id", "ou_123", "text", {"text": "hello"}, "om_source_5")]


@pytest.mark.asyncio
async def test_outbound_messenger_routes_turn_final_markdown_links_to_post() -> None:
    client = _FakeClient()
    messenger = FeishuOutboundMessenger(lambda: cast(object, client))

    ok = await messenger.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_123",
            content="**加粗** [链接](https://example.com)",
            reply_to="om_source_7",
            metadata={"feishu_delivery": "turn_final"},
        )
    )

    assert ok is True
    assert client.sent[0][2] == "post"
    paragraphs = client.sent[0][3]["zh_cn"]["content"]
    assert paragraphs[0][0]["tag"] == "text"
    assert paragraphs[0][0]["text"] == "加粗 "
    assert paragraphs[0][1] == {"tag": "a", "text": "链接", "href": "https://example.com"}


@pytest.mark.asyncio
async def test_outbound_messenger_routes_short_turn_final_text_to_text() -> None:
    client = _FakeClient()
    messenger = FeishuOutboundMessenger(lambda: cast(object, client))

    ok = await messenger.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_123",
            content="你好 :wave: 欢迎回来 :sparkles:",
            reply_to="om_source_7b",
            metadata={"feishu_delivery": "turn_final"},
        )
    )

    assert ok is True
    assert client.sent[0][2] == "text"
    assert client.sent[0][3]["text"] == "你好 👋 欢迎回来 ✨️"


@pytest.mark.asyncio
async def test_outbound_messenger_routes_long_turn_final_markdown_to_interactive() -> None:
    client = _FakeClient()
    messenger = FeishuOutboundMessenger(lambda: cast(object, client))
    content = "# 标题\n\n" + ("内容 " * 1200)

    ok = await messenger.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_123",
            content=content,
            reply_to="om_source_8",
            metadata={"feishu_delivery": "turn_final"},
        )
    )

    assert ok is True
    assert client.sent[0][2] == "interactive"
    assert client.sent[0][3]["elements"]


@pytest.mark.asyncio
async def test_outbound_messenger_uses_metadata_message_id_as_reply_target() -> None:
    client = _FakeClient()
    messenger = FeishuOutboundMessenger(lambda: cast(object, client))

    ok = await messenger.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_123",
            content="hello :wave:",
            metadata={"message_id": "om_source_meta"},
        )
    )

    assert ok is True
    assert client.sent[0][4] == "om_source_meta"
    assert client.sent[0][3]["text"] == "hello 👋"


@pytest.mark.asyncio
async def test_outbound_messenger_preserves_existing_interactive_behavior_when_not_reply_post() -> None:
    client = _FakeClient()
    messenger = FeishuOutboundMessenger(lambda: cast(object, client))

    ok = await messenger.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_123",
            content="# Heading\n\n- a\n- b",
            reply_to="om_source_9",
        )
    )

    assert ok is True
    assert client.sent[0][2] == "interactive"


@pytest.mark.asyncio
async def test_outbound_messenger_sends_notification_card_v2_when_present() -> None:
    client = _FakeClient()
    messenger = FeishuOutboundMessenger(lambda: cast(object, client))

    ok = await messenger.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_123",
            content="fallback should be ignored",
            feishu_card=FeishuCardPayload(
                template="notification",
                title="待办提醒",
                summary="你有一个待处理事项。",
                items=["补交材料", "确认时间"],
                timestamp="今天 18:00 前",
            ),
        )
    )

    assert ok is True
    assert len(client.sent) == 1
    assert client.sent[0][2] == "interactive"
    payload = client.sent[0][3]
    assert payload["schema"] == "2.0"
    assert payload["header"]["title"]["content"] == "待办提醒"
    assert payload["body"]["direction"] == "vertical"
    assert any(element["tag"] == "markdown" for element in payload["body"]["elements"])


@pytest.mark.asyncio
async def test_outbound_messenger_sends_confirm_card_v2_without_actions() -> None:
    client = _FakeClient()
    messenger = FeishuOutboundMessenger(lambda: cast(object, client))

    ok = await messenger.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_123",
            content="",
            feishu_card=FeishuCardPayload(
                template="confirm",
                title="需要你确认",
                summary="这个事项是否继续跟进？",
                items=["保留待办", "暂不处理"],
                confirm_prompt="回复 保留 或 暂停",
                suggested_replies=["保留", "暂停"],
            ),
        )
    )

    assert ok is True
    assert len(client.sent) == 1
    assert client.sent[0][2] == "interactive"
    payload = client.sent[0][3]
    assert payload["schema"] == "2.0"
    assert payload["header"]["template"] == "orange"
    assert all(element.get("tag") != "action" for element in payload["body"]["elements"])
    assert all(element.get("tag") != "action_list" for element in payload["body"]["elements"])
    assert "Reply In Chat" in payload["body"]["elements"][-1]["content"]


@pytest.mark.asyncio
async def test_outbound_messenger_falls_back_to_post_when_card_send_fails() -> None:
    client = _FakeClient()
    client.fail_interactive_sends = 1
    messenger = FeishuOutboundMessenger(lambda: cast(object, client))

    ok = await messenger.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_123",
            content="",
            feishu_card=FeishuCardPayload(
                template="notification",
                title="待办提醒",
                summary="请尽快处理。",
                items=["步骤 1", "步骤 2"],
            ),
        )
    )

    assert ok is True
    assert len(client.sent) == 2
    assert client.sent[0][2] == "interactive"
    assert client.sent[1][2] in {"post", "text"}


@pytest.mark.asyncio
async def test_outbound_messenger_sends_image_attachments(tmp_path) -> None:
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"png")
    client = _FakeClient()
    messenger = FeishuOutboundMessenger(lambda: cast(object, client))

    ok = await messenger.send(OutboundMessage(channel="feishu", chat_id="oc_chat_1", content="", media=[str(image_path)], reply_to="om_source_6"))

    assert ok is True
    assert client.uploaded_images == [str(image_path)]
    assert client.sent == [("chat_id", "oc_chat_1", "image", {"image_key": "img_key"}, "om_source_6")]


@pytest.mark.asyncio
async def test_outbound_messenger_returns_false_when_send_fails() -> None:
    client = _FakeClient()
    client.fail_send = True
    messenger = FeishuOutboundMessenger(lambda: cast(object, client))

    ok = await messenger.send(OutboundMessage(channel="feishu", chat_id="ou_123", content="hello", reply_to="om_source_10"))

    assert ok is False
