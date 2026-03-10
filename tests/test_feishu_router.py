from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.feishu.dedupe import FeishuEventDedupe, FeishuLRUDedupe, FeishuSQLiteDedupe
from nanobot.feishu.router import FeishuEnvelope, FeishuRouter
from nanobot.feishu.security import FeishuWebhookSecurity


class _Handler:
    def __init__(self):
        self.handled = []

    async def handle_message(self, envelope: FeishuEnvelope) -> None:
        self.handled.append(envelope)


@pytest.mark.asyncio
async def test_router_dedupes_same_webhook_event(tmp_path: Path) -> None:
    handler = _Handler()
    router = FeishuRouter(
        handler=handler,
        dedupe=FeishuEventDedupe(
            memory=FeishuLRUDedupe(max_size=8),
            store=FeishuSQLiteDedupe(tmp_path / "feishu-dedupe.db"),
        ),
    )
    payload = {
        "header": {
            "event_id": "evt_1",
            "event_type": "im.message.receive_v1",
            "tenant_key": "tenant-1",
        },
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "ou_user_1"},
            },
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_chat_1",
                "chat_type": "group",
                "message_type": "text",
                "content": '{"text":"hello"}',
            },
        },
    }

    envelope = FeishuEnvelope(source="webhook", payload=payload)
    await router.route(envelope)
    await router.route(envelope)

    assert len(handler.handled) == 1


@pytest.mark.asyncio
async def test_router_ignores_non_message_webhook_event(tmp_path: Path) -> None:
    handler = _Handler()
    router = FeishuRouter(
        handler=handler,
        dedupe=FeishuEventDedupe(
            memory=FeishuLRUDedupe(max_size=8),
            store=FeishuSQLiteDedupe(tmp_path / "feishu-dedupe.db"),
        ),
    )

    await router.route(
        FeishuEnvelope(
            source="webhook",
            payload={
                "header": {
                    "event_id": "evt_2",
                    "event_type": "im.message.message_read_v1",
                },
                "event": {},
            },
        )
    )

    assert handler.handled == []


def test_webhook_security_validates_token_and_challenge() -> None:
    security = FeishuWebhookSecurity(verification_token="token-123")
    payload = {
        "type": "url_verification",
        "token": "token-123",
        "challenge": "challenge-value",
    }

    assert security.is_valid(payload) is True
    assert security.build_challenge_response(payload) == {"challenge": "challenge-value"}


def test_webhook_security_rejects_wrong_token() -> None:
    security = FeishuWebhookSecurity(verification_token="token-123")

    assert security.is_valid({"token": "wrong"}) is False
