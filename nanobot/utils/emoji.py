"""Lightweight emoji helpers for chat channels."""

from __future__ import annotations

import emoji


def emojize_text(text: str) -> str:
    """Convert :alias: style emoji codes to Unicode while preserving plain text."""
    if not text:
        return text
    return emoji.emojize(text, language="alias", variant="emoji_type")
