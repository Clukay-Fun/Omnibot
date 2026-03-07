import json
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.feishu import FeishuChannel, _build_card_action_content
from nanobot.config.schema import FeishuConfig


def _build_channel() -> FeishuChannel:
    return FeishuChannel(config=FeishuConfig(), bus=MessageBus())


def test_welcome_state_uses_sqlite_store(tmp_path) -> None:
    channel = FeishuChannel(config=FeishuConfig(), bus=MessageBus(), workspace=tmp_path)

    assert channel._mark_welcome_sent("feishu:ou_a") is True
    assert channel._mark_welcome_sent("feishu:ou_a") is False

    assert channel._group_welcome_allowed("oc_group") is True
    assert channel._group_welcome_allowed("oc_group") is False

    conn = sqlite3.connect(str(tmp_path / "memory" / "feishu" / "state.sqlite3"))
    rows = conn.execute(
        "SELECT chat_id, state_key FROM feishu_chat_state"
    ).fetchall()
    conn.close()

    assert ("__global__", "welcomed:feishu:ou_a") in rows
    assert ("oc_group", "group_welcome_last_sent") in rows


def test_message_index_legacy_json_migrates_into_sqlite(tmp_path) -> None:
    feishu_dir = tmp_path / "memory" / "feishu"
    feishu_dir.mkdir(parents=True, exist_ok=True)
    (feishu_dir / "message_index.json").write_text(
        json.dumps(
            {
                "om_legacy": {
                    "content": "legacy quote",
                    "chat_id": "oc_group",
                    "source_message_id": "im_1",
                    "created_at": "2026-01-01T00:00:00",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    channel = FeishuChannel(config=FeishuConfig(), bus=MessageBus(), workspace=tmp_path)
    summary = channel._resolve_quoted_bot_summary({"upper_message_id": "om_legacy", "chat_id": "oc_group"})

    assert summary == "legacy quote"

    _ = FeishuChannel(config=FeishuConfig(), bus=MessageBus(), workspace=tmp_path)

    conn = sqlite3.connect(str(tmp_path / "memory" / "feishu" / "state.sqlite3"))
    count = conn.execute("SELECT COUNT(*) FROM feishu_message_index").fetchone()[0]
    conn.close()
    assert count == 1


def test_channel_state_legacy_json_migrates_into_sqlite(tmp_path) -> None:
    feishu_dir = tmp_path / "memory" / "feishu"
    feishu_dir.mkdir(parents=True, exist_ok=True)
    (feishu_dir / "channel_state.json").write_text(
        json.dumps(
            {
                "welcomed": {"feishu:ou_legacy": "2026-01-01T00:00:00"},
                "group_welcomes": {"oc_group": 4102444800},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    channel = FeishuChannel(config=FeishuConfig(), bus=MessageBus(), workspace=tmp_path)
    assert channel._mark_welcome_sent("feishu:ou_legacy") is False
    assert channel._group_welcome_allowed("oc_group") is False

    _ = FeishuChannel(config=FeishuConfig(), bus=MessageBus(), workspace=tmp_path)
    conn = sqlite3.connect(str(tmp_path / "memory" / "feishu" / "state.sqlite3"))
    count = conn.execute("SELECT COUNT(*) FROM feishu_chat_state").fetchone()[0]
    conn.close()
    assert count == 2


def test_sqlite_tables_initialized_on_channel_startup(tmp_path) -> None:
    _ = FeishuChannel(config=FeishuConfig(), bus=MessageBus(), workspace=tmp_path)

    conn = sqlite3.connect(str(tmp_path / "memory" / "feishu" / "state.sqlite3"))
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    conn.close()

    assert "oauth_states" in tables
    assert "feishu_user_tokens" in tables
    assert "feishu_chat_state" in tables
    assert "feishu_message_index" in tables
    assert "reminder_state" in tables
    assert "event_audit" in tables


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


def test_build_card_action_content_uses_action_name_when_value_missing() -> None:
    action = SimpleNamespace(
        tag="button",
        name="submit_onboarding",
        form_value={"tone": "standard"},
    )

    content, action_key, action_tag = _build_card_action_content(action)

    assert action_tag == "button"
    assert action_key == "submit_onboarding"
    assert "action_name: submit_onboarding" in content
    assert "action_key: submit_onboarding" in content


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
async def test_group_continuation_command_bypasses_mention_gate() -> None:
    channel = _build_channel()
    called = False

    async def _fake_handle_message(**kwargs: Any) -> None:
        nonlocal called
        called = True

    channel._handle_message = _fake_handle_message  # type: ignore[assignment]

    await channel._on_message(_build_text_event(message_id="m-g-cont", text="继续"))

    assert called is True


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
