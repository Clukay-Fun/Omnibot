"""Shared heartbeat execution result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class HeartbeatExecutionResult:
    """Structured result returned by a heartbeat execution run."""

    response_text: str
    state_summary: str
    transcript_messages: list[dict[str, Any]] = field(default_factory=list)


class HeartbeatExecutionError(RuntimeError):
    """Raised when a heartbeat execution run fails after producing transcript state."""

    def __init__(
        self,
        state_summary: str,
        transcript_messages: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(state_summary)
        self.state_summary = state_summary
        self.transcript_messages = list(transcript_messages or [])
