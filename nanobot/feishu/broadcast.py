"""Feishu one-off broadcast helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from nanobot.bus.events import OutboundMessage


@dataclass(frozen=True)
class BroadcastRecipient:
    open_id: str
    name: str


@dataclass(frozen=True)
class BroadcastResult:
    total: int
    succeeded: list[BroadcastRecipient]
    failed: list[BroadcastRecipient]


class FeishuBroadcastService:
    """Discover active Feishu users and send one-off announcements."""

    def __init__(self, client: Any, messenger: Any):
        self._client = client
        self._messenger = messenger

    @staticmethod
    def _is_active_user(user: Any) -> bool:
        open_id = str(getattr(user, "open_id", "") or "").strip()
        if not open_id:
            return False

        status = getattr(user, "status", None)
        if status is None:
            return True

        if bool(getattr(status, "is_resigned", False)):
            return False
        if bool(getattr(status, "is_exited", False)):
            return False
        if bool(getattr(status, "is_frozen", False)):
            return False
        if bool(getattr(status, "is_unjoin", False)):
            return False
        if getattr(status, "is_activated", None) is False:
            return False
        return True

    def list_active_recipients(self, page_size: int = 100, limit: int | None = None) -> list[BroadcastRecipient]:
        recipients: list[BroadcastRecipient] = []
        page_token: str | None = None

        while True:
            items, next_page_token, has_more = self._client.list_users_sync(
                page_token=page_token,
                page_size=page_size,
            )
            for user in items:
                if not self._is_active_user(user):
                    continue
                recipients.append(
                    BroadcastRecipient(
                        open_id=str(getattr(user, "open_id", "") or ""),
                        name=str(getattr(user, "name", "") or getattr(user, "open_id", "")),
                    )
                )
                if limit is not None and len(recipients) >= limit:
                    return recipients[:limit]

            if not has_more:
                break
            page_token = next_page_token
            if not page_token:
                break

        return recipients

    async def broadcast(
        self,
        content: str,
        recipients: list[BroadcastRecipient],
        *,
        throttle_seconds: float = 0.0,
    ) -> BroadcastResult:
        succeeded: list[BroadcastRecipient] = []
        failed: list[BroadcastRecipient] = []

        for idx, recipient in enumerate(recipients):
            ok = await self._messenger.send(
                OutboundMessage(channel="feishu", chat_id=recipient.open_id, content=content)
            )
            if ok:
                succeeded.append(recipient)
            else:
                failed.append(recipient)

            if throttle_seconds > 0 and idx < len(recipients) - 1:
                await asyncio.sleep(throttle_seconds)

        return BroadcastResult(total=len(recipients), succeeded=succeeded, failed=failed)
