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
        prepare_placeholder: Callable[[TranslatedFeishuMessage], Awaitable[bool] | bool] | None = None,
    ):
        self.adapter = adapter
        self.publish = publish
        self.media_loader = media_loader
        self.command_handler = command_handler
        self.memory_store = memory_store
        self.persona_manager = persona_manager
        self.prepare_placeholder = prepare_placeholder

    async def handle_message(self, envelope: Any) -> None:
        try:
            translated = self.adapter.translate_message(envelope.payload)
            if inspect.isawaitable(translated):
                translated = await translated
            if translated is None:
                return
            if self.command_handler is not None and await self.command_handler.handle(translated):
                return
            if self.prepare_placeholder is not None:
                prepared = self.prepare_placeholder(translated)
                if inspect.isawaitable(prepared):
                    await prepared
            active_overlay: OverlayContext | None = None
            if self.persona_manager is not None:
                overlay_root = self.persona_manager.overlay_root_for_chat(
                    str(translated.metadata.get("chat_type") or ""),
                    str(translated.metadata.get("tenant_key") or ""),
                    str(translated.metadata.get("user_open_id") or ""),
                )
                if overlay_root is not None:
                    bootstrap_status = None
                    if hasattr(self.persona_manager, "bootstrap_status"):
                        bootstrap_status = self.persona_manager.bootstrap_status(overlay_root)
                        include_bootstrap = bool(bootstrap_status.get("include_bootstrap"))
                    else:
                        include_bootstrap = self.persona_manager.should_include_bootstrap(overlay_root)
                    active_overlay = OverlayContext(
                        system_overlay_root=str(overlay_root),
                        system_overlay_bootstrap=include_bootstrap,
                    )
                    logger.bind(
                        event="feishu_bootstrap_decision",
                        chat_id=translated.chat_id,
                        user_open_id=str(translated.metadata.get("user_open_id") or ""),
                        overlay_root=str(overlay_root),
                        include_bootstrap=include_bootstrap,
                        has_name=bootstrap_status.get("has_name") if bootstrap_status else None,
                        has_style=bootstrap_status.get("has_style") if bootstrap_status else None,
                        has_long_term_context=bootstrap_status.get("has_long_term_context") if bootstrap_status else None,
                        has_current_work=bootstrap_status.get("has_current_work") if bootstrap_status else None,
                    ).info("Feishu bootstrap decision evaluated")
                    translated.metadata = active_overlay.to_metadata(translated.metadata)
            if self.memory_store is not None:
                extra_context = self.memory_store.safe_build_extra_context(translated.metadata)
                if active_overlay is not None and str(translated.metadata.get("chat_type") or "") != "group":
                    extra_context = [item for item in extra_context if not item.startswith("Summary:")]
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
