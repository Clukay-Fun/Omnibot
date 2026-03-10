"""Helpers for rendering phase-aware CLI progress without polluting final output."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CliProgressRenderState:
    thinking_started: bool = False
    thinking_done_announced: bool = False

    def reset(self) -> None:
        self.thinking_started = False
        self.thinking_done_announced = False


def consume_cli_progress_event(
    state: CliProgressRenderState,
    content: str | None,
    *,
    phase: str = "answer",
) -> tuple[list[str], str | None]:
    text = str(content or "").strip()
    normalized_phase = str(phase or "answer").strip().lower() or "answer"

    if normalized_phase == "thinking":
        if not text:
            return [], None
        if not state.thinking_started:
            state.thinking_started = True
            state.thinking_done_announced = False
            return ["思考中"], text
        return [], text

    if normalized_phase == "thinking_done":
        if state.thinking_done_announced:
            return [], None
        state.thinking_started = True
        state.thinking_done_announced = True
        return ["思考完成"], None

    if not text:
        return [], None
    return [text], None
