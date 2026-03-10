"""Cron data access object."""

from typing import TYPE_CHECKING, Any

from nanobot.storage.dao.base import BaseDAO

if TYPE_CHECKING:
    from nanobot.storage.sqlite_store import SQLiteStore


class CronDAO(BaseDAO):
    """
    用处: 管理后台定时任务在 SQLite 内的落盘。

    功能:
        - 增删查改任务与下一次运行的执行数据同步。
    """

    def list_all(self) -> list[dict[str, Any]]:
        rows = self.store._conn.execute(
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
                "schedule": self.store._decode(str(row["schedule_json"]), default={}),
                "payload": self.store._decode(str(row["payload_json"]), default={}),
                "state": self.store._decode(str(row["state_json"]), default={}),
                "created_at_ms": int(row["created_at_ms"]),
                "updated_at_ms": int(row["updated_at_ms"]),
                "delete_after_run": bool(row["delete_after_run"]),
            }
            for row in rows
        ]

    def upsert(self, job: dict[str, Any]) -> None:
        with self.store.transaction() as cur:
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
                    self.store._bool_to_int(job.get("enabled", True)),
                    self.store._encode(job.get("schedule") or {}),
                    self.store._encode(job.get("payload") or {}),
                    self.store._encode(job.get("state") or {}),
                    int(job.get("created_at_ms") or 0),
                    int(job.get("updated_at_ms") or 0),
                    self.store._bool_to_int(job.get("delete_after_run", False)),
                ),
            )

    def save_all(self, jobs: list[dict[str, Any]]) -> None:
        keep_ids = [str(item.get("id") or "") for item in jobs if str(item.get("id") or "")]
        with self.store.transaction() as cur:
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
                        self.store._bool_to_int(job.get("enabled", True)),
                        self.store._encode(job.get("schedule") or {}),
                        self.store._encode(job.get("payload") or {}),
                        self.store._encode(job.get("state") or {}),
                        int(job.get("created_at_ms") or 0),
                        int(job.get("updated_at_ms") or 0),
                        self.store._bool_to_int(job.get("delete_after_run", False)),
                    ),
                )

    def delete(self, job_id: str) -> None:
        with self.store.transaction() as cur:
            cur.execute("DELETE FROM cron_jobs WHERE id=?", (job_id,))
