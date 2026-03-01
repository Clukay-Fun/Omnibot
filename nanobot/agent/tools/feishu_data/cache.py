"""Small in-memory TTL cache helpers for Feishu data tools."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


@dataclass(slots=True)
class _Entry(Generic[V]):
    value: V
    expire_at: float


class TTLCache(Generic[K, V]):
    def __init__(self, ttl_seconds: int, max_entries: int = 256):
        self._ttl_seconds = max(0, int(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._store: OrderedDict[K, _Entry[V]] = OrderedDict()

    def get(self, key: K) -> V | None:
        if self._ttl_seconds <= 0:
            return None
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.expire_at <= time.time():
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return entry.value

    def set(self, key: K, value: V) -> None:
        if self._ttl_seconds <= 0:
            return
        self._store[key] = _Entry(value=value, expire_at=time.time() + self._ttl_seconds)
        self._store.move_to_end(key)
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)
