"""Session data access object."""

from typing import TYPE_CHECKING, Any

from nanobot.storage.dao.base import BaseDAO

if TYPE_CHECKING:
    from nanobot.storage.sqlite_store import SQLiteStore


class SessionDAO(BaseDAO):
    """
    用处: 处理会话状态与多轮记忆的数据库持久化策略。

    功能:
        - 替 SessionManager 从 SQLite 表 `session_state` 中同步元数据和时间戳。
    """

    def upsert(
        self,
        session_key: str,
        *,
        metadata: dict[str, Any],
        created_at: str,
        updated_at: str,
        last_consolidated: int,
    ) -> None:
        with self.store.transaction() as cur:
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
                    self.store._encode(metadata),
                    created_at,
                    updated_at,
                    int(last_consolidated),
                ),
            )

    def get(self, session_key: str) -> dict[str, Any] | None:
        row = self.store._conn.execute(
            """
            SELECT session_key, metadata, created_at, updated_at, last_consolidated
            FROM session_state
            WHERE session_key=?
            """,
            (session_key,),
        ).fetchone()
        data = self.store._row_to_dict(row)
        if data is None:
            return None
        return {
            "session_key": str(data.get("session_key") or session_key),
            "metadata": self.store._decode(data.get("metadata"), default={}),
            "created_at": str(data.get("created_at") or ""),
            "updated_at": str(data.get("updated_at") or ""),
            "last_consolidated": int(data.get("last_consolidated") or 0),
        }

    def list_all(self) -> list[dict[str, Any]]:
        rows = self.store._conn.execute(
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
                    "metadata": self.store._decode(str(row["metadata"]), default={}),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                    "last_consolidated": int(row["last_consolidated"]),
                }
            )
        return result

    def delete(self, session_key: str) -> None:
        with self.store.transaction() as cur:
            cur.execute("DELETE FROM session_state WHERE session_key=?", (session_key,))
