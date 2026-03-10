"""
描述: 对话会话历史的持久化层与多轮上下文读写管理器。
主要功能:
    - 统筹隔离各个频道的会话记录，并提供向大语言模型请求时的上下文拼接能力。
"""

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.storage.sqlite_store import SQLiteConnectionOptions, SQLiteStore
from nanobot.utils.helpers import ensure_dir, get_state_path, migrate_legacy_path, safe_filename


@dataclass
class Session:
    """
    用处: 运行时某一个具体对话维度的上下文持有器。

    功能:
        - 在内存中托管由用户问题、AI 回复以及 Tool 调用记录组成的 List[dict]。
        - 为大模型缓存命中效率考量，内部采用增量追加写入而不随便篡改历史记录。
    """

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input, aligned to a user turn."""
        unconsolidated = self.messages[self.last_consolidated:]
        sliced = unconsolidated[-max_messages:]

        # Drop leading non-user messages to avoid orphaned tool_result blocks
        for i, m in enumerate(sliced):
            if m.get("role") == "user":
                sliced = sliced[i:]
                break

        out: list[dict[str, Any]] = []
        for m in sliced:
            entry: dict[str, Any] = {"role": m["role"], "content": m.get("content", "")}
            for k in ("tool_calls", "tool_call_id", "name"):
                if k in m:
                    entry[k] = m[k]
            out.append(entry)
        return out

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()


class SessionManager:
    """
    用处: 统合全部会话的缓存调度与序列化门面。

    功能:
        - 以 JSONL 落盘方式支持海量消息的快速末尾追加读写保障。
        - 处理来自各个平台的 Session Key 生成、匹配获取、生命周期销毁驱逐。
    """

    def __init__(
        self,
        workspace: Path,
        *,
        state_db_path: Path | None = None,
        sqlite_options: SQLiteConnectionOptions | None = None,
    ):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self.legacy_sessions_dir = Path.home() / ".nanobot" / "sessions"
        self._cache: dict[str, Session] = {}
        legacy_db_path = self.workspace / "memory" / "feishu" / "state.sqlite3"
        db_path = state_db_path or (get_state_path() / "feishu" / "state.sqlite3")
        migrate_legacy_path(legacy_db_path, db_path, related_suffixes=("-wal", "-shm", ".bak"))
        self._sqlite = SQLiteStore(db_path, options=sqlite_options)

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy global session path (~/.nanobot/sessions/)."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.legacy_sessions_dir / f"{safe_key}.jsonl"

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        return session

    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        if not path.exists():
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session {} from legacy path", key)
                except Exception:
                    logger.exception("Failed to migrate session {}", key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            updated_at = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        updated_at = datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            sql_state = self._sqlite.sessions.get(key)
            if sql_state:
                sql_metadata = sql_state.get("metadata")
                if isinstance(sql_metadata, dict):
                    metadata = sql_metadata
                sql_created_at = str(sql_state.get("created_at") or "")
                sql_updated_at = str(sql_state.get("updated_at") or "")
                if sql_created_at:
                    created_at = datetime.fromisoformat(sql_created_at)
                if sql_updated_at:
                    updated_at = datetime.fromisoformat(sql_updated_at)
                last_consolidated = int(sql_state.get("last_consolidated") or 0)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                updated_at=updated_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    def save(self, session: Session) -> None:
        """Save a session to disk."""
        path = self._get_session_path(session.key)

        with open(path, "w", encoding="utf-8") as f:
            metadata_line = {
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated
            }
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        self._sqlite.sessions.upsert(
            session.key,
            metadata=dict(session.metadata or {}),
            created_at=session.created_at.isoformat(),
            updated_at=session.updated_at.isoformat(),
            last_consolidated=session.last_consolidated,
        )

        self._cache[session.key] = session

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)

    def delete(self, key: str) -> bool:
        """Delete a session file and clear cache entry."""
        path = self._get_session_path(key)
        legacy_path = self._get_legacy_session_path(key)
        exists = path.exists() or legacy_path.exists() or key in self._cache

        self._cache.pop(key, None)
        try:
            if path.exists():
                path.unlink()
            if legacy_path.exists():
                legacy_path.unlink()
            self._sqlite.sessions.delete(key)
            return exists
        except Exception as e:
            logger.warning("Failed to delete session {}: {}", key, e)
            return False

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        sessions = []
        sql_map = {item["session_key"]: item for item in self._sqlite.sessions.list_all()}

        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                # Read just the metadata line
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            key = data.get("key") or path.stem.replace("_", ":", 1)
                            sql_state = sql_map.get(key)
                            sessions.append({
                                "key": key,
                                "created_at": (
                                    sql_state.get("created_at") if sql_state else data.get("created_at")
                                ),
                                "updated_at": (
                                    sql_state.get("updated_at") if sql_state else data.get("updated_at")
                                ),
                                "path": str(path)
                            })
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
