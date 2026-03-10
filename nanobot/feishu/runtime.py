"""Feishu runtime assembly helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from nanobot.bus.queue import MessageBus
from nanobot.feishu.adapter import FeishuAdapter
from nanobot.feishu.archive import FeishuAsyncArchiveService
from nanobot.feishu.client import FeishuClient
from nanobot.feishu.commands import FeishuCommandHandler
from nanobot.feishu.dedupe import FeishuEventDedupe, FeishuLRUDedupe, FeishuSQLiteDedupe
from nanobot.feishu.handler import FeishuEventHandler
from nanobot.feishu.media import FeishuInboundMediaLoader
from nanobot.feishu.memory import FeishuUserMemoryStore
from nanobot.feishu.outbound import FeishuOutboundMessenger
from nanobot.feishu.router import FeishuRouter
from nanobot.feishu.streaming import FeishuCardStreamer
from nanobot.feishu.ttl import FeishuTTLManager

if TYPE_CHECKING:
    from collections.abc import Callable
    from nanobot.config.schema import FeishuConfig
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import SessionManager


@dataclass
class FeishuRuntime:
    outbound: FeishuOutboundMessenger
    handler: FeishuEventHandler
    router: FeishuRouter
    streaming: FeishuCardStreamer
    archive_service: FeishuAsyncArchiveService | None


def build_feishu_runtime(
    *,
    config: "FeishuConfig",
    bus: MessageBus,
    workspace: Path,
    groq_api_key: str,
    client_getter: "Callable[[], FeishuClient | None]",
    inbound_publish,
    session_manager: "SessionManager | None",
    provider: "LLMProvider | None",
    model: str | None,
    memory_window: int,
) -> FeishuRuntime:
    dedupe_path = Path(config.dedupe_db_path).expanduser() if config.dedupe_db_path else workspace / ".feishu_dedupe.sqlite3"
    memory_path = Path(config.memory_db_path).expanduser() if config.memory_db_path else workspace / ".feishu_memory.sqlite3"
    memory_store = FeishuUserMemoryStore(memory_path)
    archive_service = None
    ttl_manager = None
    if session_manager is not None and provider is not None and model is not None:
        archive_service = FeishuAsyncArchiveService(memory_store=memory_store, session_manager=session_manager, provider=provider, model=model)
        ttl_manager = FeishuTTLManager(session_manager=session_manager, memory_store=memory_store, provider=provider, model=model, ttl_seconds=config.session_ttl_seconds)

    adapter = FeishuAdapter(config, ttl_manager=ttl_manager, overflow_manager=archive_service, overflow_keep_messages=memory_window)
    command_handler = FeishuCommandHandler(memory_store=memory_store, respond=bus.publish_outbound, session_manager=session_manager, archive_service=archive_service)
    media_loader = FeishuInboundMediaLoader(client_getter, groq_api_key=groq_api_key)
    outbound = FeishuOutboundMessenger(client_getter)
    streaming = FeishuCardStreamer(
        client_getter=client_getter,
        scope=config.streaming_scope,
        throttle_seconds=config.stream_throttle_seconds,
    )
    handler = FeishuEventHandler(adapter=adapter, publish=inbound_publish, media_loader=media_loader.load_translated_media, command_handler=command_handler, memory_store=memory_store)
    router = FeishuRouter(handler=handler, dedupe=FeishuEventDedupe(memory=FeishuLRUDedupe(max_size=config.dedupe_memory_size), store=FeishuSQLiteDedupe(dedupe_path)))
    return FeishuRuntime(outbound=outbound, handler=handler, router=router, streaming=streaming, archive_service=archive_service)
