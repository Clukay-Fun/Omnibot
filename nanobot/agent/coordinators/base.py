"""Base coordinator interfaces for deterministic pre-LLM turn handling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.session.manager import Session

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop


@dataclass(slots=True)
class CoordinatorToolResult:
    final_content: str


class AgentCoordinator:
    def __init__(self, agent: AgentLoop | None = None) -> None:
        self._agent = agent

    async def handle(self, *, msg: InboundMessage, session: Session) -> OutboundMessage | None:
        _ = (msg, session)
        return None

    def on_tool_result(
        self,
        *,
        session: Session,
        tool_name: str,
        raw_args: dict[str, Any],
        result: str,
    ) -> CoordinatorToolResult | None:
        _ = (session, tool_name, raw_args, result)
        return None
