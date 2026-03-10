"""Feishu event dedupe stores."""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from pathlib import Path


class FeishuLRUDedupe:
    """Small in-memory LRU dedupe cache for hot events."""

    def __init__(self, max_size: int = 1000):
        self.max_size = max_size
        self._items: OrderedDict[str, None] = OrderedDict()

    def has(self, event_key: str) -> bool:
        if event_key not in self._items:
            return False
        self._items.move_to_end(event_key)
        return True

    def record(self, event_key: str) -> None:
        self._items[event_key] = None
        self._items.move_to_end(event_key)
        while len(self._items) > self.max_size:
            self._items.popitem(last=False)


class FeishuSQLiteDedupe:
    """Persistent dedupe store backed by SQLite."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS feishu_dedupe (event_key TEXT PRIMARY KEY, seen_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        self._conn.commit()

    def has(self, event_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM feishu_dedupe WHERE event_key = ? LIMIT 1",
            (event_key,),
        ).fetchone()
        return row is not None

    def record(self, event_key: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO feishu_dedupe(event_key) VALUES (?)",
            (event_key,),
        )
        self._conn.commit()


class FeishuEventDedupe:
    """Two-layer dedupe: in-memory LRU first, SQLite second."""

    def __init__(self, memory: FeishuLRUDedupe, store: FeishuSQLiteDedupe):
        self.memory = memory
        self.store = store

    def seen_or_record(self, event_key: str | None) -> bool:
        if not event_key:
            return False
        if self.memory.has(event_key):
            return True
        if self.store.has(event_key):
            self.memory.record(event_key)
            return True
        self.store.record(event_key)
        self.memory.record(event_key)
        return False
