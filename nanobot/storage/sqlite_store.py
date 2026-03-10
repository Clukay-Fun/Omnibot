"""
描述: 全局唯一的基础关卡数据库引擎网关。
主要功能:
    - 为飞书状态、OAuth 鉴权、Token 刷新、会话序列化及定时任务提供底层数据存取。
    - 集中管理表结构的创建迁移与执行锁。
"""

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

from nanobot.storage.dao.cron import CronDAO
from nanobot.storage.dao.oauth import OAuthDAO
from nanobot.storage.dao.reminder import ReminderDAO
from nanobot.storage.dao.session import SessionDAO


@dataclass(frozen=True, slots=True)
class SQLiteConnectionOptions:
    """
    用处: 调优数据库表现的建连参数。

    功能:
        - 精细控制 PRAGMA 级的选项，例如并发性能关键的 WAL 模式开关。
    """

    journal_mode: Literal["WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF"] = "WAL"
    synchronous: Literal["OFF", "NORMAL", "FULL", "EXTRA"] = "NORMAL"
    busy_timeout_ms: int = 5000


class SQLiteStore:
    """
    用处: DB 操作统一数据访问对象（DAO）层。

    功能:
        - 透明挂载并完成所需表结构的幂等检查（`init_db`）。
        - 为各种业务模型（UserToken / Cron / Reminders / Session 等）暴露安全的原子级 CRUD。
    """

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

        self.sessions = SessionDAO(self)
        self.oauth = OAuthDAO(self)
        self.cron = CronDAO(self)
        self.reminders = ReminderDAO(self)

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

    def get_oauth_state(self, state: str) -> dict[str, Any] | None:
        """Compatibility wrapper for older callers still reading OAuth state from SQLiteStore."""
        return self.oauth.get_state(state)

    def cleanup_expired_oauth_states(self, *, now_iso: str) -> int:
        """Compatibility wrapper for older callers invoking OAuth cleanup from SQLiteStore."""
        return self.oauth.cleanup_expired_states(now_iso=now_iso)

    def cleanup_feishu_message_index_before(self, before_at: str) -> int:
        with self.transaction() as cur:
            cur.execute("DELETE FROM feishu_message_index WHERE created_at < ?", (before_at,))
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
