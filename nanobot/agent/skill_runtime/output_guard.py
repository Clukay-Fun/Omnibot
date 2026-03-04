"""Output guard helpers with continuation cache support."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(slots=True)
class GuardResult:
    content: str | list[Any]
    truncated: bool
    continuation_token: str | None = None
    remaining_chars: int = 0
    remaining_items: int = 0


@dataclass(slots=True)
class _CacheEntry:
    payload: str | list[Any]
    expires_at: float


class ContinuationCache:
    """In-memory continuation payload cache with TTL."""

    def __init__(self, ttl_seconds: int = 600, now_fn: Callable[[], float] | None = None):
        self.ttl_seconds = ttl_seconds
        self._now_fn = now_fn or time.monotonic
        self._entries: dict[str, _CacheEntry] = {}

    def put(self, payload: str | list[Any]) -> str:
        self._purge_expired()
        token = uuid.uuid4().hex
        self._entries[token] = _CacheEntry(payload=payload, expires_at=self._now_fn() + self.ttl_seconds)
        return token

    def pop(self, token: str) -> str | list[Any] | None:
        self._purge_expired()
        entry = self._entries.pop(token, None)
        if not entry:
            return None
        if entry.expires_at <= self._now_fn():
            return None
        return entry.payload

    def _purge_expired(self) -> None:
        now = self._now_fn()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)


class OutputGuard:
    """Guard text/list payloads and attach continuation tokens when truncated."""

    def __init__(self, continuation_cache: ContinuationCache | None = None):
        self.continuation_cache = continuation_cache or ContinuationCache()

    def guard_text(self, content: str, *, max_chars: int) -> GuardResult:
        if len(content) <= max_chars:
            return GuardResult(content=content, truncated=False)

        visible = content[:max_chars]
        remaining = content[max_chars:]
        token = self.continuation_cache.put(remaining)
        return GuardResult(
            content=visible,
            truncated=True,
            continuation_token=token,
            remaining_chars=len(remaining),
        )

    def guard_items(self, items: list[Any], *, max_items: int) -> GuardResult:
        if len(items) <= max_items:
            return GuardResult(content=items, truncated=False)

        visible = items[:max_items]
        remaining = items[max_items:]
        token = self.continuation_cache.put(remaining)
        return GuardResult(
            content=visible,
            truncated=True,
            continuation_token=token,
            remaining_items=len(remaining),
        )

    def continue_from(self, token: str) -> str | list[Any] | None:
        return self.continuation_cache.pop(token)
