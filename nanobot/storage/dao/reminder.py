"""Reminder data access object."""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from nanobot.storage.dao.base import BaseDAO

if TYPE_CHECKING:
    from nanobot.storage.sqlite_store import SQLiteStore


class ReminderDAO(BaseDAO):
    """
    用处: 提醒事项业务的数据库持久化网关。

    功能:
        - 提供个人或群组提醒项的创建、查询、更新和取消等原子操作。
    """

    def list_all(
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
        rows = self.store._conn.execute(query, tuple(params)).fetchall()
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

    def upsert(self, reminder: dict[str, Any]) -> None:
        with self.store.transaction() as cur:
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

    def save_all(self, reminders: list[dict[str, Any]]) -> None:
        keep_ids = [str(item.get("id") or "") for item in reminders if str(item.get("id") or "")]
        with self.store.transaction() as cur:
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

    def update(self, reminder_id: str, *, updates: dict[str, Any]) -> dict[str, Any] | None:
        current = self.get(reminder_id)
        if current is None:
            return None
        current.update(updates)
        self.upsert(current)
        return current

    def cancel(self, reminder_id: str, *, cancelled_at: str) -> dict[str, Any] | None:
        return self.update(reminder_id, updates={"status": "cancelled", "cancelled_at": cancelled_at})

    def get(self, reminder_id: str) -> dict[str, Any] | None:
        rows = self.list_all()
        for item in rows:
            if item.get("id") == reminder_id:
                return item
        return None

    def get_by_external_key(self, external_key: str) -> dict[str, Any] | None:
        rows = self.list_all(external_key=external_key)
        return rows[0] if rows else None
