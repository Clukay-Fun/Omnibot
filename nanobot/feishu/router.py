"""Route Feishu ingress events into the handler pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nanobot.feishu.payload import read_path


@dataclass
class FeishuEnvelope:
    """Raw ingress event wrapper shared by webhook and websocket paths."""

    source: str
    payload: Any
    metadata: dict[str, Any] = field(default_factory=dict)


class FeishuRouter:
    """Route Feishu ingress envelopes to the appropriate handler."""

    MESSAGE_EVENT_TYPE = "im.message.receive_v1"

    def __init__(self, handler: Any, dedupe: Any | None = None):
        self.handler = handler
        self.dedupe = dedupe

    async def route(self, envelope: FeishuEnvelope) -> bool:
        if not self._is_message_event(envelope):
            return False
        event_key = self.get_event_key(envelope)
        if self.dedupe and self.dedupe.seen_or_record(event_key):
            return False
        await self.handler.handle_message(envelope)
        return True

    def get_event_key(self, envelope: FeishuEnvelope) -> str | None:
        payload = envelope.payload
        if envelope.source == "webhook":
            header = read_path(payload, "header")
            if isinstance(header, dict) and header.get("event_id"):
                return str(header["event_id"])
        message_id = read_path(payload, "event", "message", "message_id")
        if message_id:
            return f"message:{message_id}"
        return None

    def _is_message_event(self, envelope: FeishuEnvelope) -> bool:
        if envelope.source == "websocket":
            return read_path(envelope.payload, "event", "message") is not None
        event_type = read_path(envelope.payload, "header", "event_type")
        return event_type == self.MESSAGE_EVENT_TYPE
