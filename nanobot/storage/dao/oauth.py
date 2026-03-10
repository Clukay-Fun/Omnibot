"""OAuth data access object."""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from nanobot.storage.dao.base import BaseDAO

if TYPE_CHECKING:
    from nanobot.storage.sqlite_store import SQLiteStore


class OAuthDAO(BaseDAO):
    """
    用处: 处理 OAuth2.0 授权全生命周期的状态读写。

    功能:
        - 创建、查询状态，声明使用消费，以及定时清理过期拦截状态。
    """

    def upsert_state(
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
        with self.store.transaction() as cur:
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
                    self.store._encode(scopes),
                    status,
                    self.store._encode(payload or {}),
                ),
            )

    def get_state(self, state: str) -> dict[str, Any] | None:
        row = self.store._conn.execute(
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
        data = self.store._row_to_dict(row)
        if data is None:
            return None
        data["scopes"] = self.store._decode(data.get("scopes"), default=[])
        data["payload"] = self.store._decode(data.get("payload"), default={})
        return data

    def claim_state(self, state: str, *, now_iso: str) -> dict[str, Any] | None:
        with self.store.transaction() as cur:
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

            data = self.store._row_to_dict(row)
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

            data["scopes"] = self.store._decode(data.get("scopes"), default=[])
            data["payload"] = self.store._decode(data.get("payload"), default={})
            return data

    def finalize_state(self, state: str, *, status: str, last_error: str | None = None) -> None:
        consumed_at = datetime.now().isoformat() if status == "consumed" else None
        with self.store.transaction() as cur:
            cur.execute(
                """
                UPDATE oauth_states
                SET status=?, consumed_at=?, last_error=?
                WHERE state=?
                """,
                (status, consumed_at, last_error, state),
            )

    def cleanup_expired_states(self, *, now_iso: str) -> int:
        with self.store.transaction() as cur:
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
