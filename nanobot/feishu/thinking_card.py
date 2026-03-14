"""Helpers for rendering Feishu thinking-card payloads."""

from __future__ import annotations

from typing import Final

_MAX_ENTRIES: Final[int] = 6
_ZERO_WIDTH_SPACE: Final[str] = "\u200b"
_QUOTE_PREFIX: Final[str] = "▏ "


def build_initial() -> dict:
    """Build the initial neutral placeholder card."""
    return {
        "config": {"wide_screen_mode": True},
        "elements": [_entry_element("…")],
    }


def build_progress(entries: list[str]) -> dict:
    """Build an in-progress thinking card."""
    return {
        "config": {"wide_screen_mode": True},
        "elements": _render_entries(entries),
    }


def build_completed(entries: list[str]) -> dict:
    """Build a completed thinking card that preserves meaningful status entries."""
    return {
        "config": {"wide_screen_mode": True},
        "elements": _render_entries([*_trim_entries(entries), "思考完成"]),
    }


def build_minimal() -> dict:
    """Build the weakest-possible card payload for empty-turn cleanup."""
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": _ZERO_WIDTH_SPACE}],
            }
        ],
    }


def _render_entries(entries: list[str]) -> list[dict]:
    trimmed = _trim_entries(entries)
    if not trimmed:
        return [_entry_element("…")]
    return [_entry_element(entry) for entry in trimmed]


def _trim_entries(entries: list[str]) -> list[str]:
    return [entry for entry in entries if entry][- _MAX_ENTRIES :]


def _entry_element(entry: str) -> dict:
    return {
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": f"{_QUOTE_PREFIX}{entry}"}],
    }
