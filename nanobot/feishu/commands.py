"""Feishu shell-level commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from nanobot.bus.events import OutboundMessage
from nanobot.feishu.archive import FeishuAsyncArchiveService
from nanobot.feishu.memory import FeishuUserMemoryStore
from nanobot.feishu.types import TranslatedFeishuMessage
from nanobot.session.manager import SessionManager


class FeishuCommandHandler:
    """Handle Feishu-specific commands before publishing inbound messages."""

    def __init__(
        self,
        memory_store: FeishuUserMemoryStore,
        respond: Callable[[OutboundMessage], Awaitable[None]],
        session_manager: SessionManager | None = None,
        archive_service: FeishuAsyncArchiveService | None = None,
    ):
        self.memory_store = memory_store
        self.respond = respond
        self.session_manager = session_manager
        self.archive_service = archive_service

    @staticmethod
    def _reply_to(translated: TranslatedFeishuMessage) -> str | None:
        reply_to = translated.metadata.get("message_id")
        return str(reply_to) if reply_to else None

    async def handle(self, translated: TranslatedFeishuMessage) -> bool:
        command = translated.content.strip().lower()
        if command in {"/clear", "/new"}:
            tenant_key = str(translated.metadata.get("tenant_key") or "")
            user_open_id = str(translated.metadata.get("user_open_id") or "")
            if (
                self.session_manager is not None
                and self.archive_service is not None
                and translated.session_key
                and tenant_key
                and user_open_id
            ):
                await self.archive_service.queue_clear_archive(
                    translated.session_key,
                    tenant_key,
                    user_open_id,
                )
            await self.respond(
                OutboundMessage(
                    channel="feishu",
                    chat_id=translated.chat_id,
                    content="Cleared this short-term session. I will finish archiving recent context in the background.",
                    reply_to=self._reply_to(translated),
                )
            )
            return True

        if command == "/help":
            await self.respond(
                OutboundMessage(
                    channel="feishu",
                    chat_id=translated.chat_id,
                    content=(
                        "Feishu commands:\n"
                        "/help — Show available commands\n"
                        "/clear — Start a new short-term session\n"
                        "/forget — Delete your Feishu long-term memory\n"
                        "/new — Start a new conversation\n"
                        "/stop — Stop the current task"
                    ),
                    reply_to=self._reply_to(translated),
                )
            )
            return True

        if command == "/forget":
            tenant_key = str(translated.metadata.get("tenant_key") or "")
            user_open_id = str(translated.metadata.get("user_open_id") or "")
            if tenant_key and user_open_id:
                self.memory_store.clear(tenant_key, user_open_id)
            await self.respond(
                OutboundMessage(
                    channel="feishu",
                    chat_id=translated.chat_id,
                    content="Forgot your Feishu long-term memory for this tenant.",
                    reply_to=self._reply_to(translated),
                )
            )
            return True

        return False
