"""Helpers for reading Feishu payload structures."""

from __future__ import annotations

from typing import Any


def read_path(value: Any, *path: str) -> Any:
    """Read a nested path from dict-backed or attribute-backed payload objects."""
    current = value
    for part in path:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
    return current
