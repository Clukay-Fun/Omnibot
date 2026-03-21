"""Structured Feishu user memory storage and context helpers."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass
class FeishuUserMemory:
    """Structured long-term memory for one tenant user."""

    tenant_key: str
    user_open_id: str
    profile: str = ""
    summary: str = ""


@dataclass
class FeishuMemorySnapshot:
    """Persisted archive snapshot waiting to be merged into long-term memory."""

    snapshot_id: int
    tenant_key: str
    user_open_id: str
    session_key: str
    reason: str
    start_index: int
    end_index: int
    messages: list[dict[str, Any]]
    status: str = "pending"


class FeishuUserMemoryStore:
    """SQLite-backed shared user memory keyed by tenant + user_open_id."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feishu_user_memory (
                tenant_key TEXT NOT NULL,
                user_open_id TEXT NOT NULL,
                profile TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_key, user_open_id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feishu_memory_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_key TEXT NOT NULL,
                user_open_id TEXT NOT NULL,
                session_key TEXT NOT NULL,
                reason TEXT NOT NULL,
                start_index INTEGER NOT NULL,
                end_index INTEGER NOT NULL,
                messages_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._conn.commit()

    def get(self, tenant_key: str, user_open_id: str) -> FeishuUserMemory | None:
        row = self._conn.execute(
            "SELECT tenant_key, user_open_id, profile, summary FROM feishu_user_memory WHERE tenant_key = ? AND user_open_id = ?",
            (tenant_key, user_open_id),
        ).fetchone()
        if row is None:
            return None
        return FeishuUserMemory(
            tenant_key=row[0],
            user_open_id=row[1],
            profile=row[2] or "",
            summary=row[3] or "",
        )

    def upsert(self, tenant_key: str, user_open_id: str, *, profile: str = "", summary: str = "") -> None:
        self._conn.execute(
            """
            INSERT INTO feishu_user_memory(tenant_key, user_open_id, profile, summary)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tenant_key, user_open_id)
            DO UPDATE SET profile = excluded.profile, summary = excluded.summary, updated_at = CURRENT_TIMESTAMP
            """,
            (tenant_key, user_open_id, profile, summary),
        )
        self._conn.commit()

    def clear(self, tenant_key: str, user_open_id: str) -> None:
        self._conn.execute(
            "DELETE FROM feishu_user_memory WHERE tenant_key = ? AND user_open_id = ?",
            (tenant_key, user_open_id),
        )
        self._conn.commit()

    def clear_all_for_user(self, tenant_key: str, user_open_id: str) -> None:
        """Delete long-term memory and queued archive snapshots for one Feishu user."""
        self._conn.execute(
            "DELETE FROM feishu_user_memory WHERE tenant_key = ? AND user_open_id = ?",
            (tenant_key, user_open_id),
        )
        self._conn.execute(
            "DELETE FROM feishu_memory_snapshots WHERE tenant_key = ? AND user_open_id = ?",
            (tenant_key, user_open_id),
        )
        self._conn.commit()

    def list_tenant_keys(self, user_open_id: str) -> list[str]:
        """Return distinct tenant keys known for this user."""
        rows = self._conn.execute(
            """
            SELECT DISTINCT tenant_key
            FROM (
                SELECT tenant_key FROM feishu_user_memory WHERE user_open_id = ?
                UNION
                SELECT tenant_key FROM feishu_memory_snapshots WHERE user_open_id = ?
            )
            ORDER BY tenant_key ASC
            """,
            (user_open_id, user_open_id),
        ).fetchall()
        return [str(row[0]) for row in rows if row and row[0]]

    def enqueue_snapshot(
        self,
        *,
        tenant_key: str,
        user_open_id: str,
        session_key: str,
        reason: str,
        start_index: int,
        end_index: int,
        messages: list[dict[str, Any]],
    ) -> FeishuMemorySnapshot:
        cursor = self._conn.execute(
            """
            INSERT INTO feishu_memory_snapshots(
                tenant_key, user_open_id, session_key, reason, start_index, end_index, messages_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                tenant_key,
                user_open_id,
                session_key,
                reason,
                start_index,
                end_index,
                json.dumps(messages, ensure_ascii=False),
            ),
        )
        self._conn.commit()
        return FeishuMemorySnapshot(
            snapshot_id=int(cursor.lastrowid),
            tenant_key=tenant_key,
            user_open_id=user_open_id,
            session_key=session_key,
            reason=reason,
            start_index=start_index,
            end_index=end_index,
            messages=list(messages),
        )

    def claim_next_snapshot(self) -> FeishuMemorySnapshot | None:
        row = self._conn.execute(
            """
            SELECT id, tenant_key, user_open_id, session_key, reason, start_index, end_index, messages_json, status
            FROM feishu_memory_snapshots
            WHERE status = 'pending'
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            "UPDATE feishu_memory_snapshots SET status = 'running', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (row[0],),
        )
        self._conn.commit()
        return FeishuMemorySnapshot(
            snapshot_id=row[0],
            tenant_key=row[1],
            user_open_id=row[2],
            session_key=row[3],
            reason=row[4],
            start_index=row[5],
            end_index=row[6],
            messages=json.loads(row[7]),
            status="running",
        )

    def mark_snapshot_done(self, snapshot_id: int) -> None:
        self._conn.execute(
            "UPDATE feishu_memory_snapshots SET status = 'done', updated_at = CURRENT_TIMESTAMP, error = '' WHERE id = ?",
            (snapshot_id,),
        )
        self._conn.commit()

    def mark_snapshot_failed(self, snapshot_id: int, error: str) -> None:
        self._conn.execute(
            "UPDATE feishu_memory_snapshots SET status = 'failed', updated_at = CURRENT_TIMESTAMP, error = ? WHERE id = ?",
            (error[:500], snapshot_id),
        )
        self._conn.commit()

    def reset_running_snapshots(self) -> None:
        self._conn.execute(
            "UPDATE feishu_memory_snapshots SET status = 'pending', updated_at = CURRENT_TIMESTAMP WHERE status = 'running'"
        )
        self._conn.commit()

    def count_snapshots(self, status: str | None = None) -> int:
        if status is None:
            row = self._conn.execute("SELECT COUNT(*) FROM feishu_memory_snapshots").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM feishu_memory_snapshots WHERE status = ?",
                (status,),
            ).fetchone()
        return int(row[0] if row else 0)

    def build_extra_context(self, metadata: dict[str, Any]) -> list[str]:
        tenant_key = str(metadata.get("tenant_key") or "")
        user_open_id = str(metadata.get("user_open_id") or "")
        if not tenant_key or not user_open_id:
            return []

        record = self.get(tenant_key, user_open_id)
        if record is None:
            return []

        context: list[str] = []
        if record.profile.strip():
            context.append(f"Profile: {record.profile.strip()}")

        chat_type = str(metadata.get("chat_type") or "")
        if chat_type != "group" and record.summary.strip():
            context.append(f"Summary: {record.summary.strip()}")
        return context

    def safe_build_extra_context(self, metadata: dict[str, Any]) -> list[str]:
        try:
            return self.build_extra_context(metadata)
        except Exception:
            logger.exception("Failed to load Feishu user memory, continuing without extra context")
            return []
