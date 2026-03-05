import json
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.feishu import FeishuChannel, _build_card_action_content
from nanobot.config.schema import FeishuConfig


def _build_channel() -> FeishuChannel:
    return FeishuChannel(config=FeishuConfig(), bus=MessageBus())


def _build_text_event(
    *,
    message_id: str,
    text: str,
    chat_type: str = "group",
    sender_id: str = "ou_sender",
    mentions: list[Any] | None = None,
    thread_id: str | None = None,
) -> Any:
    return SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id=message_id,
                chat_id="oc_chat_1",
                chat_type=chat_type,
                message_type="text",
                content=json.dumps({"text": text}, ensure_ascii=False),
                mentions=mentions,
                thread_id=thread_id,
                root_id=thread_id,
                parent_id=thread_id,
            ),
            sender=SimpleNamespace(
                sender_type="user",
                sender_id=SimpleNamespace(open_id=sender_id),
            ),
        )
    )


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


@pytest.mark.asyncio
async def test_group_message_requires_mention_by_default() -> None:
    channel = _build_channel()
    called = False

    async def _fake_handle_message(**kwargs: Any) -> None:
        nonlocal called
        called = True

    channel._handle_message = _fake_handle_message  # type: ignore[assignment]

    await channel._on_message(_build_text_event(message_id="m-g-1", text="hello"))

    assert called is False


@pytest.mark.asyncio
async def test_group_message_allows_payload_mention_signal() -> None:
    channel = _build_channel()
    called = False

    async def _fake_handle_message(**kwargs: Any) -> None:
        nonlocal called
        called = True

    channel._handle_message = _fake_handle_message  # type: ignore[assignment]

    await channel._on_message(
        _build_text_event(
            message_id="m-g-2",
            text="hello",
            mentions=[{"name": "bot"}],
        )
    )

    assert called is True


@pytest.mark.asyncio
async def test_group_message_allows_admin_prefix_bypass() -> None:
    channel = FeishuChannel(
        config=FeishuConfig(
            activation_admin_open_ids=["ou_admin"],
            activation_admin_prefix_bypass="/bot",
        ),
        bus=MessageBus(),
    )
    called = False

    async def _fake_handle_message(**kwargs: Any) -> None:
        nonlocal called
        called = True

    channel._handle_message = _fake_handle_message  # type: ignore[assignment]

    await channel._on_message(_build_text_event(message_id="m-g-3", text="/bot run", sender_id="ou_admin"))

    assert called is True


@pytest.mark.asyncio
async def test_group_message_allows_continuation_without_mention() -> None:
    channel = _build_channel()
    captured: dict[str, Any] = {}

    async def _fake_handle_message(**kwargs: Any) -> None:
        captured.update(kwargs)

    channel._handle_message = _fake_handle_message  # type: ignore[assignment]

    await channel._on_message(_build_text_event(message_id="m-g-continue", text="继续"))

    assert captured["content"] == "继续"


@pytest.mark.asyncio
async def test_topic_message_is_always_activated_by_default() -> None:
    channel = _build_channel()
    called = False

    async def _fake_handle_message(**kwargs: Any) -> None:
        nonlocal called
        called = True

    channel._handle_message = _fake_handle_message  # type: ignore[assignment]

    await channel._on_message(
        _build_text_event(
            message_id="m-topic-1",
            text="hello",
            thread_id="omt_topic_1",
        )
    )

    assert called is True
