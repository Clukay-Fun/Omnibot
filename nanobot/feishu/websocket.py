"""Feishu WebSocket ingress helpers."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

from loguru import logger

from nanobot.config.schema import FeishuConfig


def register_optional_event(builder: Any, method_name: str, handler: Any) -> Any:
    """Register an event handler only when the SDK supports it."""
    method = getattr(builder, method_name, None)
    return method(handler) if callable(method) else builder


class FeishuWebSocketBridge:
    """Manage the Feishu long-connection WebSocket client and reconnect loop."""

    def __init__(
        self,
        config: FeishuConfig,
        on_message_sync: Callable[[Any], None],
        on_reaction_created: Callable[[Any], None],
        on_message_read: Callable[[Any], None],
        on_bot_p2p_chat_entered: Callable[[Any], None],
    ):
        self.config = config
        self.on_message_sync = on_message_sync
        self.on_reaction_created = on_reaction_created
        self.on_message_read = on_message_read
        self.on_bot_p2p_chat_entered = on_bot_p2p_chat_entered
        self._running = False
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None

    def start(self) -> None:
        """Create the Feishu WebSocket client and start the reconnect loop thread."""
        import lark_oapi as lark

        self._running = True
        builder = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(self.on_message_sync)
        builder = register_optional_event(
            builder,
            "register_p2_im_message_reaction_created_v1",
            self.on_reaction_created,
        )
        builder = register_optional_event(
            builder,
            "register_p2_im_message_message_read_v1",
            self.on_message_read,
        )
        builder = register_optional_event(
            builder,
            "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
            self.on_bot_p2p_chat_entered,
        )
        event_handler = builder.build()

        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        def run_ws() -> None:
            import time
            import lark_oapi.ws.client as lark_ws_client

            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)
            lark_ws_client.loop = ws_loop
            try:
                while self._running:
                    try:
                        self._ws_client.start()
                    except Exception as e:
                        logger.warning("Feishu WebSocket error: {}", e)
                    if self._running:
                        time.sleep(5)
            finally:
                ws_loop.close()

        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()

    def stop(self) -> None:
        """Request the reconnect loop to stop."""
        self._running = False
