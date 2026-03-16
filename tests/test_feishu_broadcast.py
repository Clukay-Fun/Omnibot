from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.feishu.broadcast import BroadcastRecipient, FeishuBroadcastService


class _FakeClient:
    def __init__(self, pages: list[tuple[list[object], str | None, bool]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str | None, int]] = []

    def list_users_sync(self, *, page_token: str | None = None, page_size: int = 100):
        self.calls.append((page_token, page_size))
        return self.pages.pop(0)


class _FakeMessenger:
    def __init__(self, failing_open_ids: set[str] | None = None) -> None:
        self.failing_open_ids = failing_open_ids or set()
        self.sent: list[tuple[str, str]] = []

    async def send(self, msg) -> bool:
        self.sent.append((msg.chat_id, msg.content))
        return msg.chat_id not in self.failing_open_ids


def _user(
    open_id: str | None,
    name: str,
    *,
    is_activated: bool = True,
    is_resigned: bool = False,
    is_exited: bool = False,
    is_frozen: bool = False,
    is_unjoin: bool = False,
):
    return SimpleNamespace(
        open_id=open_id,
        name=name,
        status=SimpleNamespace(
            is_activated=is_activated,
            is_resigned=is_resigned,
            is_exited=is_exited,
            is_frozen=is_frozen,
            is_unjoin=is_unjoin,
        ),
    )


def test_active_recipients_skip_resigned_or_inactive_users() -> None:
    client = _FakeClient(
        [
            (
                [
                    _user("ou_active_1", "Alice"),
                    _user("ou_resigned", "Bob", is_resigned=True),
                    _user(None, "NoOpenId"),
                ],
                "next-token",
                True,
            ),
            (
                [
                    _user("ou_active_2", "Carol"),
                    _user("ou_unjoin", "Dave", is_unjoin=True),
                    _user("ou_frozen", "Eve", is_frozen=True),
                ],
                None,
                False,
            ),
        ]
    )
    service = FeishuBroadcastService(client=client, messenger=_FakeMessenger())

    recipients = service.list_active_recipients(page_size=50)

    assert recipients == [
        BroadcastRecipient(open_id="ou_active_1", name="Alice"),
        BroadcastRecipient(open_id="ou_active_2", name="Carol"),
    ]
    assert client.calls == [(None, 50), ("next-token", 50)]


@pytest.mark.asyncio
async def test_broadcast_send_collects_successes_and_failures() -> None:
    recipients = [
        BroadcastRecipient(open_id="ou_ok", name="Alice"),
        BroadcastRecipient(open_id="ou_fail", name="Bob"),
    ]
    messenger = _FakeMessenger(failing_open_ids={"ou_fail"})
    service = FeishuBroadcastService(client=_FakeClient([]), messenger=messenger)

    result = await service.broadcast("上线通知", recipients, throttle_seconds=0)

    assert result.total == 2
    assert result.succeeded == [BroadcastRecipient(open_id="ou_ok", name="Alice")]
    assert result.failed == [BroadcastRecipient(open_id="ou_fail", name="Bob")]
    assert messenger.sent == [("ou_ok", "上线通知"), ("ou_fail", "上线通知")]
