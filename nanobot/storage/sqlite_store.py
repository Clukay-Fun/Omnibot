"""SQLite storage primitives for Feishu state, OAuth, and token lifecycle."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from shutil import copy2
from typing import Any, Iterator, Literal


@dataclass(frozen=True, slots=True)
class SQLiteConnectionOptions:
    """Connection-level sqlite pragma settings."""

    journal_mode: Literal["WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF"] = "WAL"
    synchronous: Literal["OFF", "NORMAL", "FULL", "EXTRA"] = "NORMAL"
    busy_timeout_ms: int = 5000


class SQLiteStore:
    """Unified SQLite storage with schema + transaction helpers."""

    GLOBAL_CHAT_ID = "__global__"

    def __init__(self, db_path: Path, *, options: SQLiteConnectionOptions | None = None):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._options = options or SQLiteConnectionOptions()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._configure_connection()
        self.init_db()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _configure_connection(self) -> None:
        with self._lock:
            journal_mode = self._normalize_journal_mode(self._options.journal_mode)
            synchronous = self._normalize_synchronous(self._options.synchronous)
            busy_timeout_ms = max(0, int(self._options.busy_timeout_ms))

            self._conn.execute(f"PRAGMA journal_mode={journal_mode}")
            self._conn.execute(f"PRAGMA synchronous={synchronous}")
            self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.commit()

    @staticmethod
    def _normalize_journal_mode(raw: str) -> str:
        mode = str(raw or "WAL").upper()
        if mode in {"WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF"}:
            return mode
        return "WAL"

    @staticmethod
    def _normalize_synchronous(raw: str) -> str:
        mode = str(raw or "NORMAL").upper()
        if mode in {"OFF", "NORMAL", "FULL", "EXTRA"}:
            return mode
        return "NORMAL"

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        """Run statements in a commit/rollback transaction."""
        with self._lock:
            cursor = self._conn.cursor()
            try:
                yield cursor
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cursor.close()

    def _table_columns(self, table: str) -> set[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _ensure_column(self, table: str, column_name: str, definition: str) -> None:
        if column_name in self._table_columns(table):
            return
        with self.transaction() as cur:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def init_db(self) -> None:
        """Initialize all SQLite tables (idempotent)."""
        with self.transaction() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state TEXT PRIMARY KEY,
                    provider TEXT NOT NULL DEFAULT 'feishu',
                    actor_open_id TEXT,
                    chat_id TEXT,
                    thread_id TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    redirect_uri TEXT,
                    scopes TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    consumed_at TEXT,
                    last_error TEXT,
                    payload TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS feishu_user_tokens (
                    open_id TEXT PRIMARY KEY,
                    app_id TEXT,
                    access_token TEXT,
                    refresh_token TEXT,
                    token_type TEXT,
                    expires_at TEXT,
                    refresh_expires_at TEXT,
                    scope TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    last_refreshed_at TEXT,
                    last_error TEXT,
                    payload TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS feishu_chat_state (
                    chat_id TEXT NOT NULL,
                    state_key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, state_key)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS feishu_message_index (
                    message_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_message_id TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS reminder_state (
                    reminder_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS event_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT,
                    event_id TEXT,
                    chat_id TEXT,
                    message_id TEXT,
                    payload TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cron_jobs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    schedule_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    delete_after_run INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY,
                    external_key TEXT,
                    user_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    channel TEXT,
                    text TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT,
                    cancelled_at TEXT,
                    calendar_event_id TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS session_state (
                    session_key TEXT PRIMARY KEY,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_consolidated INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_feishu_message_index_created_at ON feishu_message_index(created_at)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_audit_event_type ON event_audit(event_type, created_at)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_state_updated_at ON session_state(updated_at)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_cron_jobs_enabled_next_run ON cron_jobs(enabled, updated_at_ms)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_reminders_user_due ON reminders(user_id, due_at)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_reminders_external_key ON reminders(external_key)"
            )

        self._ensure_column("oauth_states", "provider", "provider TEXT NOT NULL DEFAULT 'feishu'")
        self._ensure_column("oauth_states", "actor_open_id", "actor_open_id TEXT")
        self._ensure_column("oauth_states", "chat_id", "chat_id TEXT")
        self._ensure_column("oauth_states", "thread_id", "thread_id TEXT")
        self._ensure_column("oauth_states", "scopes", "scopes TEXT")
        self._ensure_column("oauth_states", "status", "status TEXT NOT NULL DEFAULT 'pending'")
        self._ensure_column("oauth_states", "consumed_at", "consumed_at TEXT")
        self._ensure_column("oauth_states", "last_error", "last_error TEXT")

        self._ensure_column("feishu_user_tokens", "app_id", "app_id TEXT")
        self._ensure_column("feishu_user_tokens", "refresh_expires_at", "refresh_expires_at TEXT")
        self._ensure_column("feishu_user_tokens", "status", "status TEXT NOT NULL DEFAULT 'active'")
        self._ensure_column("feishu_user_tokens", "last_refreshed_at", "last_refreshed_at TEXT")
        self._ensure_column("feishu_user_tokens", "last_error", "last_error TEXT")

        with self.transaction() as cur:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_oauth_states_status_expires_at ON oauth_states(status, expires_at)"
            )

    @staticmethod
    def _encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _decode(value: str | None, *, default: Any = None) -> Any:
        if value is None:
            return default
        try:
            return json.loads(value)
        except Exception:
            return value

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    @staticmethod
    def _bool_to_int(value: Any) -> int:
        return 1 if bool(value) else 0

    def maybe_backup_file(self, path: Path) -> Path | None:
        """Create a one-time .bak backup when source exists."""
        if not path.exists():
            return None
        backup_path = path.with_name(f"{path.name}.bak")
        if backup_path.exists():
            return backup_path
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        copy2(path, backup_path)
        return backup_path

    def list_cron_jobs(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT
                id,
                name,
                enabled,
                schedule_json,
                payload_json,
                state_json,
                created_at_ms,
                updated_at_ms,
                delete_after_run
            FROM cron_jobs
            ORDER BY created_at_ms ASC, id ASC
            """
        ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "name": str(row["name"]),
                "enabled": bool(row["enabled"]),
                "schedule": self._decode(str(row["schedule_json"]), default={}),
                "payload": self._decode(str(row["payload_json"]), default={}),
                "state": self._decode(str(row["state_json"]), default={}),
                "created_at_ms": int(row["created_at_ms"]),
                "updated_at_ms": int(row["updated_at_ms"]),
                "delete_after_run": bool(row["delete_after_run"]),
            }
            for row in rows
        ]

    def upsert_cron_job(self, job: dict[str, Any]) -> None:
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO cron_jobs(
                    id,
                    name,
                    enabled,
                    schedule_json,
                    payload_json,
                    state_json,
                    created_at_ms,
                    updated_at_ms,
                    delete_after_run
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id)
                DO UPDATE SET
                    name=excluded.name,
                    enabled=excluded.enabled,
                    schedule_json=excluded.schedule_json,
                    payload_json=excluded.payload_json,
                    state_json=excluded.state_json,
                    created_at_ms=excluded.created_at_ms,
                    updated_at_ms=excluded.updated_at_ms,
                    delete_after_run=excluded.delete_after_run
                """,
                (
                    str(job.get("id") or ""),
                    str(job.get("name") or ""),
                    self._bool_to_int(job.get("enabled", True)),
                    self._encode(job.get("schedule") or {}),
                    self._encode(job.get("payload") or {}),
                    self._encode(job.get("state") or {}),
                    int(job.get("created_at_ms") or 0),
                    int(job.get("updated_at_ms") or 0),
                    self._bool_to_int(job.get("delete_after_run", False)),
                ),
            )

    def save_cron_jobs(self, jobs: list[dict[str, Any]]) -> None:
        keep_ids = [str(item.get("id") or "") for item in jobs if str(item.get("id") or "")]
        with self.transaction() as cur:
            if keep_ids:
                placeholders = ", ".join("?" for _ in keep_ids)
                cur.execute(f"DELETE FROM cron_jobs WHERE id NOT IN ({placeholders})", keep_ids)
            else:
                cur.execute("DELETE FROM cron_jobs")

            for job in jobs:
                cur.execute(
                    """
                    INSERT INTO cron_jobs(
                        id,
                        name,
                        enabled,
                        schedule_json,
                        payload_json,
                        state_json,
                        created_at_ms,
                        updated_at_ms,
                        delete_after_run
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id)
                    DO UPDATE SET
                        name=excluded.name,
                        enabled=excluded.enabled,
                        schedule_json=excluded.schedule_json,
                        payload_json=excluded.payload_json,
                        state_json=excluded.state_json,
                        created_at_ms=excluded.created_at_ms,
                        updated_at_ms=excluded.updated_at_ms,
                        delete_after_run=excluded.delete_after_run
                    """,
                    (
                        str(job.get("id") or ""),
                        str(job.get("name") or ""),
                        self._bool_to_int(job.get("enabled", True)),
                        self._encode(job.get("schedule") or {}),
                        self._encode(job.get("payload") or {}),
                        self._encode(job.get("state") or {}),
                        int(job.get("created_at_ms") or 0),
                        int(job.get("updated_at_ms") or 0),
                        self._bool_to_int(job.get("delete_after_run", False)),
                    ),
                )

    def delete_cron_job(self, job_id: str) -> None:
        with self.transaction() as cur:
            cur.execute("DELETE FROM cron_jobs WHERE id=?", (job_id,))

    def list_reminders(
        self,
        *,
        user_id: str | None = None,
        external_key: str | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT id, external_key, user_id, chat_id, channel, text, due_at, status, created_at, "
            "updated_at, cancelled_at, calendar_event_id FROM reminders"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        if external_key is not None:
            clauses.append("external_key=?")
            params.append(external_key)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY due_at ASC, id ASC"
        rows = self._conn.execute(query, tuple(params)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = {
                "id": str(row["id"]),
                "user_id": str(row["user_id"]),
                "chat_id": str(row["chat_id"]),
                "channel": row["channel"],
                "text": str(row["text"]),
                "due_at": str(row["due_at"]),
                "status": str(row["status"]),
                "created_at": str(row["created_at"]),
            }
            if row["external_key"] is not None:
                item["external_key"] = str(row["external_key"])
            if row["updated_at"] is not None:
                item["updated_at"] = str(row["updated_at"])
            if row["cancelled_at"] is not None:
                item["cancelled_at"] = str(row["cancelled_at"])
            if row["calendar_event_id"] is not None:
                item["calendar_event_id"] = str(row["calendar_event_id"])
            result.append(item)
        return result

    def upsert_reminder(self, reminder: dict[str, Any]) -> None:
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO reminders(
                    id,
                    external_key,
                    user_id,
                    chat_id,
                    channel,
                    text,
                    due_at,
                    status,
                    created_at,
                    updated_at,
                    cancelled_at,
                    calendar_event_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id)
                DO UPDATE SET
                    external_key=excluded.external_key,
                    user_id=excluded.user_id,
                    chat_id=excluded.chat_id,
                    channel=excluded.channel,
                    text=excluded.text,
                    due_at=excluded.due_at,
                    status=excluded.status,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    cancelled_at=excluded.cancelled_at,
                    calendar_event_id=excluded.calendar_event_id
                """,
                (
                    str(reminder.get("id") or ""),
                    reminder.get("external_key"),
                    str(reminder.get("user_id") or ""),
                    str(reminder.get("chat_id") or ""),
                    reminder.get("channel"),
                    str(reminder.get("text") or ""),
                    str(reminder.get("due_at") or ""),
                    str(reminder.get("status") or "active"),
                    str(reminder.get("created_at") or datetime.now().isoformat()),
                    reminder.get("updated_at"),
                    reminder.get("cancelled_at"),
                    reminder.get("calendar_event_id"),
                ),
            )

    def save_reminders(self, reminders: list[dict[str, Any]]) -> None:
        keep_ids = [str(item.get("id") or "") for item in reminders if str(item.get("id") or "")]
        with self.transaction() as cur:
            if keep_ids:
                placeholders = ", ".join("?" for _ in keep_ids)
                cur.execute(f"DELETE FROM reminders WHERE id NOT IN ({placeholders})", keep_ids)
            else:
                cur.execute("DELETE FROM reminders")

            for reminder in reminders:
                cur.execute(
                    """
                    INSERT INTO reminders(
                        id,
                        external_key,
                        user_id,
                        chat_id,
                        channel,
                        text,
                        due_at,
                        status,
                        created_at,
                        updated_at,
                        cancelled_at,
                        calendar_event_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id)
                    DO UPDATE SET
                        external_key=excluded.external_key,
                        user_id=excluded.user_id,
                        chat_id=excluded.chat_id,
                        channel=excluded.channel,
                        text=excluded.text,
                        due_at=excluded.due_at,
                        status=excluded.status,
                        created_at=excluded.created_at,
                        updated_at=excluded.updated_at,
                        cancelled_at=excluded.cancelled_at,
                        calendar_event_id=excluded.calendar_event_id
                    """,
                    (
                        str(reminder.get("id") or ""),
                        reminder.get("external_key"),
                        str(reminder.get("user_id") or ""),
                        str(reminder.get("chat_id") or ""),
                        reminder.get("channel"),
                        str(reminder.get("text") or ""),
                        str(reminder.get("due_at") or ""),
                        str(reminder.get("status") or "active"),
                        str(reminder.get("created_at") or datetime.now().isoformat()),
                        reminder.get("updated_at"),
                        reminder.get("cancelled_at"),
                        reminder.get("calendar_event_id"),
                    ),
                )

    def update_reminder(self, reminder_id: str, *, updates: dict[str, Any]) -> dict[str, Any] | None:
        current = self.get_reminder(reminder_id)
        if current is None:
            return None
        current.update(updates)
        self.upsert_reminder(current)
        return current

    def cancel_reminder(self, reminder_id: str, *, cancelled_at: str) -> dict[str, Any] | None:
        return self.update_reminder(reminder_id, updates={"status": "cancelled", "cancelled_at": cancelled_at})

    def get_reminder(self, reminder_id: str) -> dict[str, Any] | None:
        rows = self.list_reminders()
        for item in rows:
            if item.get("id") == reminder_id:
                return item
        return None

    def get_reminder_by_external_key(self, external_key: str) -> dict[str, Any] | None:
        rows = self.list_reminders(external_key=external_key)
        return rows[0] if rows else None

    def record_event_audit(
        self,
        event_type: str,
        *,
        event_id: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.record_event_audit_batch(
            [
                {
                    "event_type": event_type,
                    "event_id": event_id,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "payload": payload or {},
                }
            ]
        )

    def record_event_audit_batch(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        now_iso = datetime.now().isoformat()
        with self.transaction() as cur:
            rows = [
                (
                    str(item.get("event_type") or ""),
                    str(item.get("event_id") or "") or None,
                    str(item.get("chat_id") or "") or None,
                    str(item.get("message_id") or "") or None,
                    self._encode(item.get("payload") or {}),
                    str(item.get("created_at") or now_iso),
                )
                for item in events
            ]
            cur.executemany(
                """
                INSERT INTO event_audit(event_type, event_id, chat_id, message_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def query_event_audit(
        self,
        *,
        event_type: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if event_type is not None:
            clauses.append("event_type=?")
            params.append(event_type)
        if chat_id is not None:
            clauses.append("chat_id=?")
            params.append(chat_id)
        if message_id is not None:
            clauses.append("message_id=?")
            params.append(message_id)
        if start_at is not None:
            clauses.append("created_at>=?")
            params.append(start_at)
        if end_at is not None:
            clauses.append("created_at<=?")
            params.append(end_at)

        safe_limit = max(1, int(limit))
        safe_offset = max(0, int(offset))
        query = "SELECT id, event_type, event_id, chat_id, message_id, payload, created_at FROM event_audit"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([safe_limit, safe_offset])

        rows = self._conn.execute(query, tuple(params)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "id": int(row["id"]),
                    "event_type": str(row["event_type"]),
                    "event_id": row["event_id"],
                    "chat_id": row["chat_id"],
                    "message_id": row["message_id"],
                    "payload": self._decode(str(row["payload"]), default={}),
                    "created_at": str(row["created_at"]),
                }
            )
        return result

    def cleanup_event_audit_before(self, before_at: str) -> int:
        with self.transaction() as cur:
            cur.execute("DELETE FROM event_audit WHERE created_at < ?", (before_at,))
            return cur.rowcount

    def cleanup_feishu_message_index_before(self, before_at: str) -> int:
        with self.transaction() as cur:
            cur.execute("DELETE FROM feishu_message_index WHERE created_at < ?", (before_at,))
            return cur.rowcount

    def upsert_session_state(
        self,
        session_key: str,
        *,
        metadata: dict[str, Any],
        created_at: str,
        updated_at: str,
        last_consolidated: int,
    ) -> None:
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO session_state(session_key, metadata, created_at, updated_at, last_consolidated)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_key)
                DO UPDATE SET
                    metadata=excluded.metadata,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    last_consolidated=excluded.last_consolidated
                """,
                (
                    session_key,
                    self._encode(metadata),
                    created_at,
                    updated_at,
                    int(last_consolidated),
                ),
            )

    def get_session_state(self, session_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT session_key, metadata, created_at, updated_at, last_consolidated
            FROM session_state
            WHERE session_key=?
            """,
            (session_key,),
        ).fetchone()
        data = self._row_to_dict(row)
        if data is None:
            return None
        return {
            "session_key": str(data.get("session_key") or session_key),
            "metadata": self._decode(data.get("metadata"), default={}),
            "created_at": str(data.get("created_at") or ""),
            "updated_at": str(data.get("updated_at") or ""),
            "last_consolidated": int(data.get("last_consolidated") or 0),
        }

    def list_session_state(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT session_key, metadata, created_at, updated_at, last_consolidated
            FROM session_state
            ORDER BY updated_at DESC
            """
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "session_key": str(row["session_key"]),
                    "metadata": self._decode(str(row["metadata"]), default={}),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                    "last_consolidated": int(row["last_consolidated"]),
                }
            )
        return result

    def delete_session_state(self, session_key: str) -> None:
        with self.transaction() as cur:
            cur.execute("DELETE FROM session_state WHERE session_key=?", (session_key,))

    def upsert_oauth_state(
        self,
        state: str,
        *,
        provider: str,
        actor_open_id: str | None,
        chat_id: str | None,
        thread_id: str | None,
        redirect_uri: str,
        scopes: list[str],
        created_at: str,
        expires_at: str,
        status: str = "pending",
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO oauth_states(
                    state, provider, actor_open_id, chat_id, thread_id,
                    created_at, expires_at, redirect_uri, scopes, status,
                    consumed_at, last_error, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                ON CONFLICT(state)
                DO UPDATE SET
                    provider=excluded.provider,
                    actor_open_id=excluded.actor_open_id,
                    chat_id=excluded.chat_id,
                    thread_id=excluded.thread_id,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at,
                    redirect_uri=excluded.redirect_uri,
                    scopes=excluded.scopes,
                    status=excluded.status,
                    consumed_at=NULL,
                    last_error=NULL,
                    payload=excluded.payload
                """,
                (
                    state,
                    provider,
                    actor_open_id,
                    chat_id,
                    thread_id,
                    created_at,
                    expires_at,
                    redirect_uri,
                    self._encode(scopes),
                    status,
                    self._encode(payload or {}),
                ),
            )

    def get_oauth_state(self, state: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT
                state, provider, actor_open_id, chat_id, thread_id,
                created_at, expires_at, redirect_uri, scopes,
                status, consumed_at, last_error, payload
            FROM oauth_states
            WHERE state=?
            """,
            (state,),
        ).fetchone()
        data = self._row_to_dict(row)
        if data is None:
            return None
        data["scopes"] = self._decode(data.get("scopes"), default=[])
        data["payload"] = self._decode(data.get("payload"), default={})
        return data

    def claim_oauth_state(self, state: str, *, now_iso: str) -> dict[str, Any] | None:
        with self.transaction() as cur:
            row = cur.execute(
                """
                SELECT
                    state, provider, actor_open_id, chat_id, thread_id,
                    created_at, expires_at, redirect_uri, scopes,
                    status, consumed_at, last_error, payload
                FROM oauth_states
                WHERE state=?
                """,
                (state,),
            ).fetchone()
            if row is None:
                return None

            data = self._row_to_dict(row)
            if data is None:
                return None

            status = str(data.get("status") or "pending")
            expires_at = str(data.get("expires_at") or "")
            if status != "pending":
                return None
            if expires_at and expires_at <= now_iso:
                cur.execute(
                    "UPDATE oauth_states SET status='expired', last_error='state expired' WHERE state=?",
                    (state,),
                )
                return None

            cur.execute(
                "UPDATE oauth_states SET status='processing', last_error=NULL WHERE state=? AND status='pending'",
                (state,),
            )
            if cur.rowcount != 1:
                return None

            data["scopes"] = self._decode(data.get("scopes"), default=[])
            data["payload"] = self._decode(data.get("payload"), default={})
            return data

    def finalize_oauth_state(self, state: str, *, status: str, last_error: str | None = None) -> None:
        consumed_at = datetime.now().isoformat() if status == "consumed" else None
        with self.transaction() as cur:
            cur.execute(
                """
                UPDATE oauth_states
                SET status=?, consumed_at=?, last_error=?
                WHERE state=?
                """,
                (status, consumed_at, last_error, state),
            )

    def cleanup_expired_oauth_states(self, *, now_iso: str) -> int:
        with self.transaction() as cur:
            cur.execute(
                """
                UPDATE oauth_states
                SET status='expired', last_error=COALESCE(last_error, 'state expired')
                WHERE status IN ('pending', 'processing')
                    AND expires_at IS NOT NULL
                    AND expires_at <= ?
                """,
                (now_iso,),
            )
            return cur.rowcount

    def upsert_feishu_user_token(
        self,
        open_id: str,
        *,
        app_id: str,
        access_token: str,
        refresh_token: str,
        token_type: str,
        scope: str,
        expires_at: str,
        refresh_expires_at: str | None,
        status: str,
        last_refreshed_at: str | None,
        last_error: str | None,
        payload: dict[str, Any] | None,
    ) -> None:
        now_iso = datetime.now().isoformat()
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO feishu_user_tokens(
                    open_id, app_id, access_token, refresh_token, token_type,
                    expires_at, refresh_expires_at, scope, status,
                    last_refreshed_at, last_error, payload, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(open_id)
                DO UPDATE SET
                    app_id=excluded.app_id,
                    access_token=excluded.access_token,
                    refresh_token=excluded.refresh_token,
                    token_type=excluded.token_type,
                    expires_at=excluded.expires_at,
                    refresh_expires_at=excluded.refresh_expires_at,
                    scope=excluded.scope,
                    status=excluded.status,
                    last_refreshed_at=excluded.last_refreshed_at,
                    last_error=excluded.last_error,
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (
                    open_id,
                    app_id,
                    access_token,
                    refresh_token,
                    token_type,
                    expires_at,
                    refresh_expires_at,
                    scope,
                    status,
                    last_refreshed_at,
                    last_error,
                    self._encode(payload or {}),
                    now_iso,
                ),
            )

    def get_feishu_user_token(self, open_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT
                open_id, app_id, access_token, refresh_token, token_type,
                expires_at, refresh_expires_at, scope, status,
                last_refreshed_at, last_error, payload, updated_at
            FROM feishu_user_tokens
            WHERE open_id=?
            """,
            (open_id,),
        ).fetchone()
        data = self._row_to_dict(row)
        if data is None:
            return None
        data["payload"] = self._decode(data.get("payload"), default={})
        return data

    def update_feishu_user_token_status(self, open_id: str, *, status: str, last_error: str | None = None) -> None:
        with self.transaction() as cur:
            cur.execute(
                """
                UPDATE feishu_user_tokens
                SET status=?, last_error=?, updated_at=?
                WHERE open_id=?
                """,
                (status, last_error, datetime.now().isoformat(), open_id),
            )

    def upsert_feishu_chat_state(self, chat_id: str, state_key: str, value: Any) -> None:
        payload = self._encode(value)
        now = datetime.now().isoformat()
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO feishu_chat_state(chat_id, state_key, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, state_key)
                DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (chat_id, state_key, payload, now),
            )

    def get_feishu_chat_state(self, chat_id: str, state_key: str, *, default: Any = None) -> Any:
        row = self._conn.execute(
            "SELECT value FROM feishu_chat_state WHERE chat_id=? AND state_key=?",
            (chat_id, state_key),
        ).fetchone()
        if row is None:
            return default
        return self._decode(str(row["value"]), default=default)

    def list_feishu_chat_state(self, chat_id: str, *, prefix: str | None = None) -> dict[str, Any]:
        if prefix:
            rows = self._conn.execute(
                "SELECT state_key, value FROM feishu_chat_state WHERE chat_id=? AND state_key LIKE ?",
                (chat_id, f"{prefix}%"),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT state_key, value FROM feishu_chat_state WHERE chat_id=?",
                (chat_id,),
            ).fetchall()
        return {str(row["state_key"]): self._decode(str(row["value"])) for row in rows}

    def list_feishu_state_by_key(self, state_key: str) -> dict[str, Any]:
        rows = self._conn.execute(
            "SELECT chat_id, value FROM feishu_chat_state WHERE state_key=?",
            (state_key,),
        ).fetchall()
        return {str(row["chat_id"]): self._decode(str(row["value"])) for row in rows}

    def upsert_feishu_message_index(
        self,
        message_id: str,
        *,
        chat_id: str,
        content: str,
        source_message_id: str | None,
        created_at: str,
    ) -> None:
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO feishu_message_index(message_id, chat_id, content, source_message_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(message_id)
                DO UPDATE SET
                    chat_id=excluded.chat_id,
                    content=excluded.content,
                    source_message_id=excluded.source_message_id,
                    created_at=excluded.created_at
                """,
                (message_id, chat_id, content, source_message_id, created_at),
            )

    def get_feishu_message_index(self, message_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT message_id, chat_id, content, source_message_id, created_at FROM feishu_message_index WHERE message_id=?",
            (message_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "message_id": str(row["message_id"]),
            "chat_id": str(row["chat_id"]),
            "content": str(row["content"]),
            "source_message_id": row["source_message_id"],
            "created_at": str(row["created_at"]),
        }

    def list_feishu_message_index(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT message_id, chat_id, content, source_message_id, created_at
            FROM feishu_message_index
            ORDER BY rowid ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "message_id": str(row["message_id"]),
                "chat_id": str(row["chat_id"]),
                "content": str(row["content"]),
                "source_message_id": row["source_message_id"],
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def delete_feishu_message_index(self, message_id: str) -> None:
        with self.transaction() as cur:
            cur.execute("DELETE FROM feishu_message_index WHERE message_id=?", (message_id,))

    def trim_feishu_message_index(self, max_entries: int) -> None:
        with self.transaction() as cur:
            cur.execute(
                """
                DELETE FROM feishu_message_index
                WHERE message_id IN (
                    SELECT message_id
                    FROM feishu_message_index
                    ORDER BY rowid DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (max_entries,),
            )

    def migrate_legacy_feishu_json(self, workspace: Path) -> None:
        """Import legacy JSON state into SQLite (idempotent)."""
        feishu_memory = workspace / "memory" / "feishu"
        self._migrate_channel_state(feishu_memory / "channel_state.json")
        self._migrate_message_index(feishu_memory / "message_index.json")

    def _migrate_channel_state(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return

        raw_welcomed = payload.get("welcomed")
        welcomed: dict[str, Any]
        if isinstance(raw_welcomed, dict):
            welcomed = dict(raw_welcomed)
        else:
            welcomed = {}
        for key, value in welcomed.items():
            self.upsert_feishu_chat_state(self.GLOBAL_CHAT_ID, f"welcomed:{key}", value)

        raw_group_welcomes = payload.get("group_welcomes")
        group_welcomes: dict[str, Any]
        if isinstance(raw_group_welcomes, dict):
            group_welcomes = dict(raw_group_welcomes)
        else:
            group_welcomes = {}
        for chat_id, value in group_welcomes.items():
            self.upsert_feishu_chat_state(str(chat_id), "group_welcome_last_sent", value)

        report = payload.get("event_registration_report")
        if isinstance(report, list):
            self.upsert_feishu_chat_state(self.GLOBAL_CHAT_ID, "event_registration_report", report)

    def _migrate_message_index(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return

        for message_id, raw in payload.items():
            if not isinstance(raw, dict):
                continue
            created_at = str(raw.get("created_at") or datetime.now().isoformat())
            self.upsert_feishu_message_index(
                str(message_id),
                chat_id=str(raw.get("chat_id") or ""),
                content=str(raw.get("content") or ""),
                source_message_id=str(raw.get("source_message_id") or "") or None,
                created_at=created_at,
            )
