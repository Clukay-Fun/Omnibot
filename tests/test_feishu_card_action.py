from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.feishu import FeishuChannel, _build_card_action_content
from nanobot.config.schema import FeishuConfig


def _build_channel() -> FeishuChannel:
    return FeishuChannel(config=FeishuConfig(), bus=MessageBus())


def test_build_card_action_content_extracts_structured_fields() -> None:
    action = SimpleNamespace(
        tag="button",
        value={"action_key": "approve", "ticket": "T-1"},
        form_value={"comment": "looks good"},
    )

    content, action_key, action_tag = _build_card_action_content(action)

    assert content.startswith("[feishu card action trigger]")
    assert "action_tag: button" in content
    assert "action_key: approve" in content
    assert '"ticket": "T-1"' in content
    assert '"comment": "looks good"' in content
    assert action_key == "approve"
    assert action_tag == "button"


@pytest.mark.asyncio
async def test_on_card_action_routes_callback_via_handle_message() -> None:
    channel = _build_channel()
    captured: dict[str, Any] = {}

    async def _fake_handle_message(**kwargs: Any) -> None:
        captured.update(kwargs)

    channel._handle_message = _fake_handle_message  # type: ignore[assignment]

    payload = SimpleNamespace(
        header=SimpleNamespace(event_type="card.action.trigger"),
        event=SimpleNamespace(
            action=SimpleNamespace(
                tag="select_static",
                value={"action": "pick_env", "env": "prod"},
                form_value={"reason": "manual"},
            ),
            context=SimpleNamespace(
                open_message_id="om_123",
                open_chat_id="oc_456",
                chat_type="group",
            ),
            operator=SimpleNamespace(operator_id=SimpleNamespace(open_id="ou_789")),
        ),
    )

    await channel._on_card_action(payload)

    assert captured["sender_id"] == "ou_789"
    assert captured["chat_id"] == "oc_456"
    assert captured["metadata"]["source_event_type"] == "card.action.trigger"
    assert captured["metadata"]["msg_type"] == "card_action"
    assert captured["metadata"]["message_id"] == "om_123"
    assert captured["metadata"]["open_message_id"] == "om_123"
    assert captured["metadata"]["action_tag"] == "select_static"
    assert captured["metadata"]["action_key"] == "pick_env"
    assert "action_value:" in captured["content"]
    assert "form_value:" in captured["content"]


@pytest.mark.asyncio
async def test_on_card_action_ignores_malformed_payload() -> None:
    channel = _build_channel()
    called = False

    async def _fake_handle_message(**kwargs: Any) -> None:
        nonlocal called
        called = True

    channel._handle_message = _fake_handle_message  # type: ignore[assignment]

    await channel._on_card_action(SimpleNamespace())
    await channel._on_card_action(SimpleNamespace(event=SimpleNamespace()))

    assert called is False
