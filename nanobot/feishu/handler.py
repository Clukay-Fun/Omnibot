"""Feishu event handlers between router and bus publication."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from nanobot.agent.overlay import OverlayContext
from nanobot.feishu.memory import FeishuUserMemoryStore
from nanobot.feishu.types import TranslatedFeishuMessage


class FeishuEventHandler:
    """Translate Feishu envelopes and publish normalized messages downstream."""

    def __init__(
        self,
        adapter: Any,
        publish: Callable[..., Awaitable[None]],
        media_loader: Callable[[Any, TranslatedFeishuMessage], Awaitable[None]] | None = None,
        command_handler: Any | None = None,
        memory_store: FeishuUserMemoryStore | None = None,
        persona_manager: Any | None = None,
    ):
        self.adapter = adapter
        self.publish = publish
        self.media_loader = media_loader
        self.command_handler = command_handler
        self.memory_store = memory_store
        self.persona_manager = persona_manager

    async def handle_message(self, envelope: Any) -> None:
        try:
            translated = self.adapter.translate_message(envelope.payload)
            if inspect.isawaitable(translated):
                translated = await translated
            if translated is None:
                return
            if self.command_handler is not None and await self.command_handler.handle(translated):
                return
            if self.persona_manager is not None:
                overlay_root = self.persona_manager.overlay_root_for_chat(
                    str(translated.metadata.get("chat_type") or ""),
                    str(translated.metadata.get("tenant_key") or ""),
                    str(translated.metadata.get("user_open_id") or ""),
                )
                if overlay_root is not None:
                    overlay_context = OverlayContext(
                        system_overlay_root=str(overlay_root),
                        system_overlay_bootstrap=self.persona_manager.should_include_bootstrap(overlay_root),
                    )
                    translated.metadata = overlay_context.to_metadata(translated.metadata)
            if self.memory_store is not None:
                extra_context = self.memory_store.safe_build_extra_context(translated.metadata)
                if extra_context:
                    translated.metadata = dict(translated.metadata)
                    translated.metadata["extra_context"] = extra_context
            if self.media_loader is not None:
                await self.media_loader(envelope, translated)
            await self.publish(
                sender_id=translated.sender_id,
                chat_id=translated.chat_id,
                content=translated.content,
                media=translated.media,
                metadata=translated.metadata,
                session_key=translated.session_key,
            )
        except Exception:
            logger.exception("Error handling Feishu event")
