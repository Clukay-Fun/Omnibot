"""Thin Feishu channel shim that wires the Feishu pipeline into BaseChannel."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import FeishuConfig
from nanobot.feishu.client import FeishuClient
from nanobot.feishu.parser import _extract_post_content
from nanobot.feishu.router import FeishuEnvelope, FeishuRouter
from nanobot.feishu.runtime import build_feishu_runtime
from nanobot.feishu.security import FeishuWebhookSecurity
from nanobot.feishu.webhook import FeishuWebhookServer
from nanobot.feishu.websocket import FeishuWebSocketBridge

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import SessionManager

FEISHU_AVAILABLE = importlib.util.find_spec("lark_oapi") is not None
__all__ = ["FeishuChannel", "_extract_post_content"]


class FeishuChannel(BaseChannel):
    name = "feishu"

    def __init__(
        self,
        config: FeishuConfig,
        bus: MessageBus,
        groq_api_key: str = "",
        gateway_host: str = "0.0.0.0",
        gateway_port: int = 18790,
        workspace: Path | None = None,
        session_manager: "SessionManager | None" = None,
        provider: "LLMProvider | None" = None,
        model: str | None = None,
        memory_window: int = 0,
    ):
        super().__init__(config, bus)
        self.config = config
        self.groq_api_key = groq_api_key
        self.gateway_host = gateway_host
        self.gateway_port = gateway_port
        self.workspace = workspace or Path(".")
        self._client: FeishuClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_bridge: FeishuWebSocketBridge | None = None
        self._webhook_server: FeishuWebhookServer | None = None
        runtime = build_feishu_runtime(config=config, bus=bus, workspace=self.workspace, groq_api_key=groq_api_key, client_getter=lambda: self._client, inbound_publish=self._handle_message, session_manager=session_manager, provider=provider, model=model, memory_window=memory_window)
        self._outbound = runtime.outbound
        self._streaming = runtime.streaming
        self._archive_service = runtime.archive_service
        self._handler = runtime.handler
        self._router: FeishuRouter = runtime.router

    async def start(self) -> None:
        if not FEISHU_AVAILABLE:
            logger.error("Feishu SDK not installed. Run: pip install lark-oapi")
            return
        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id and app_secret not configured")
            return

        self._running = True
        self._loop = asyncio.get_running_loop()
        self._client = FeishuClient.build(self.config)
        if self._archive_service is not None:
            self._archive_service.resume_pending()
        if self.config.mode in {"websocket", "hybrid"}:
            self._ws_bridge = FeishuWebSocketBridge(config=self.config, on_message_sync=self._on_message_sync, on_reaction_created=lambda _data: None, on_message_read=lambda _data: None, on_bot_p2p_chat_entered=lambda _data: logger.debug("Bot entered p2p chat (user opened chat window)"))
            self._ws_bridge.start()
        if self.config.mode in {"webhook", "hybrid"}:
            self._webhook_server = FeishuWebhookServer(host=self.gateway_host, port=self.gateway_port, path=self.config.webhook_path, security=FeishuWebhookSecurity(self.config.verification_token), router=self._router)
            self._webhook_server.start(self._loop)
            logger.info("Feishu webhook listening on {}:{}{}", self.gateway_host, self.gateway_port, self.config.webhook_path)
        logger.info("Feishu bot started in {} mode", self.config.mode)
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        self._running = False
        await self._streaming.wait_for_idle()
        if self._ws_bridge is not None:
            self._ws_bridge.stop()
        if self._webhook_server is not None:
            self._webhook_server.stop()
        logger.info("Feishu bot stopped")

    async def send(self, msg: OutboundMessage) -> None:
        metadata = dict(msg.metadata or {})
        if metadata.get("_progress"):
            await self._streaming.handle(msg)
            return

        turn_id = str(metadata.get("turn_id") or "")
        if turn_id:
            local_metadata = dict(metadata)
            local_metadata["feishu_delivery"] = "turn_final"
            delivery_msg = OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=msg.content,
                reply_to=msg.reply_to,
                media=list(msg.media),
                metadata=local_metadata,
                feishu_card=None,
            )
            if await self._outbound.send(delivery_msg):
                await self._streaming.complete_turn(turn_id)
                if self._archive_service is not None:
                    self._archive_service.kick_worker()
            return

        await self._outbound.send(msg)

    def _on_message_sync(self, data: Any) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._router.route(FeishuEnvelope(source="websocket", payload=data)), self._loop)
