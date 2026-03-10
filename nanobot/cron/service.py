"""
描述: 智能体常时驻留的后台任务调度引擎。
主要功能:
    - 基于 asyncio 管理并维持定时唤醒逻辑。
    - 负责读取并执行 SQLite 中的持久化定时任务。
    - 向下层提供时间表达式的计算，向上层暴露操作接口。
"""

import asyncio
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Sequence

from loguru import logger

from nanobot.cron.types import CronJob, CronJobState, CronPayload, CronSchedule, CronStore
from nanobot.storage.sqlite_store import SQLiteStore
from nanobot.utils.helpers import migrate_legacy_path


def _now_ms() -> int:
    return int(time.time() * 1000)


def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    """Compute next run time in ms."""
    if schedule.kind == "at":
        return schedule.at_ms if schedule.at_ms and schedule.at_ms > now_ms else None

    if schedule.kind == "every":
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        # Next interval from now
        return now_ms + schedule.every_ms

    if schedule.kind == "cron" and schedule.expr:
        try:
            from zoneinfo import ZoneInfo

            from croniter import croniter
            # Use caller-provided reference time for deterministic scheduling
            base_time = now_ms / 1000
            tz = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo
            base_dt = datetime.fromtimestamp(base_time, tz=tz)
            cron = croniter(schedule.expr, base_dt)
            next_dt = cron.get_next(datetime)
            return int(next_dt.timestamp() * 1000)
        except Exception:
            return None

    return None


def _validate_schedule_for_add(schedule: CronSchedule) -> None:
    """Validate schedule fields that would otherwise create non-runnable jobs."""
    if schedule.tz and schedule.kind != "cron":
        raise ValueError("tz can only be used with cron schedules")

    if schedule.kind == "cron" and schedule.tz:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(schedule.tz)
        except Exception:
            raise ValueError(f"unknown timezone '{schedule.tz}'") from None


class CronService:
    """
    用处: 驻留内存的调度大管家。

    功能:
        - 维护一份从 SQLite 拉取的本地调度表。
        - 伴随 Event Loop 持续运行，在时间到达之际唤起 Agent 回调。
    """

    def __init__(
        self,
        store_path: Path,
        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None,
        *,
        legacy_store_paths: Sequence[Path] | None = None,
    ):
        self.store_path = store_path
        self.on_job = on_job  # Callback to execute job, returns response text
        self._store: CronStore | None = None
        for legacy_path in legacy_store_paths or ():
            migrate_legacy_path(
                legacy_path,
                self.store_path,
                related_suffixes=(".migrated", ".bak", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".sqlite3.bak"),
            )
        self._sqlite = SQLiteStore(self.store_path.with_suffix(".sqlite3"))
        self._timer_task: asyncio.Task | None = None
        self._running = False

    def _migrate_legacy_json_if_needed(self) -> None:
        if not self.store_path.exists():
            return

        marker_path = self.store_path.with_name(f"{self.store_path.name}.migrated")
        if marker_path.exists():
            return

        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read legacy cron JSON store: {}", exc)
            return

        jobs_payload = data.get("jobs", []) if isinstance(data, dict) else []
        migrated_count = 0
        if isinstance(jobs_payload, list):
            for raw in jobs_payload:
                if not isinstance(raw, dict):
                    continue
                job_id = str(raw.get("id") or "")
                if not job_id:
                    continue
                self._sqlite.cron.upsert(
                    {
                        "id": job_id,
                        "name": str(raw.get("name") or ""),
                        "enabled": bool(raw.get("enabled", True)),
                        "schedule": {
                            "kind": str(raw.get("schedule", {}).get("kind") or "every"),
                            "atMs": raw.get("schedule", {}).get("atMs"),
                            "everyMs": raw.get("schedule", {}).get("everyMs"),
                            "expr": raw.get("schedule", {}).get("expr"),
                            "tz": raw.get("schedule", {}).get("tz"),
                        },
                        "payload": {
                            "kind": str(raw.get("payload", {}).get("kind") or "agent_turn"),
                            "message": str(raw.get("payload", {}).get("message") or ""),
                            "deliver": bool(raw.get("payload", {}).get("deliver", False)),
                            "channel": raw.get("payload", {}).get("channel"),
                            "to": raw.get("payload", {}).get("to"),
                        },
                        "state": {
                            "nextRunAtMs": raw.get("state", {}).get("nextRunAtMs"),
                            "lastRunAtMs": raw.get("state", {}).get("lastRunAtMs"),
                            "lastStatus": raw.get("state", {}).get("lastStatus"),
                            "lastError": raw.get("state", {}).get("lastError"),
                        },
                        "created_at_ms": int(raw.get("createdAtMs") or 0),
                        "updated_at_ms": int(raw.get("updatedAtMs") or 0),
                        "delete_after_run": bool(raw.get("deleteAfterRun", False)),
                    }
                )
                migrated_count += 1

        self._sqlite.maybe_backup_file(self.store_path)
        marker_path.write_text(datetime.now().isoformat(), encoding="utf-8")
        logger.info("Migrated {} legacy cron jobs from {}", migrated_count, self.store_path)

    @staticmethod
    def _cron_job_from_row(row: dict[str, Any]) -> CronJob:
        schedule = row.get("schedule") if isinstance(row.get("schedule"), dict) else {}
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        state = row.get("state") if isinstance(row.get("state"), dict) else {}
        return CronJob(
            id=str(row.get("id") or ""),
            name=str(row.get("name") or ""),
            enabled=bool(row.get("enabled", True)),
            schedule=CronSchedule(
                kind=schedule.get("kind", "every"),
                at_ms=schedule.get("atMs"),
                every_ms=schedule.get("everyMs"),
                expr=schedule.get("expr"),
                tz=schedule.get("tz"),
            ),
            payload=CronPayload(
                kind=payload.get("kind", "agent_turn"),
                message=payload.get("message", ""),
                deliver=payload.get("deliver", False),
                channel=payload.get("channel"),
                to=payload.get("to"),
            ),
            state=CronJobState(
                next_run_at_ms=state.get("nextRunAtMs"),
                last_run_at_ms=state.get("lastRunAtMs"),
                last_status=state.get("lastStatus"),
                last_error=state.get("lastError"),
            ),
            created_at_ms=int(row.get("created_at_ms") or 0),
            updated_at_ms=int(row.get("updated_at_ms") or 0),
            delete_after_run=bool(row.get("delete_after_run", False)),
        )

    @staticmethod
    def _cron_job_to_row(job: CronJob) -> dict[str, Any]:
        return {
            "id": job.id,
            "name": job.name,
            "enabled": job.enabled,
            "schedule": {
                "kind": job.schedule.kind,
                "atMs": job.schedule.at_ms,
                "everyMs": job.schedule.every_ms,
                "expr": job.schedule.expr,
                "tz": job.schedule.tz,
            },
            "payload": {
                "kind": job.payload.kind,
                "message": job.payload.message,
                "deliver": job.payload.deliver,
                "channel": job.payload.channel,
                "to": job.payload.to,
            },
            "state": {
                "nextRunAtMs": job.state.next_run_at_ms,
                "lastRunAtMs": job.state.last_run_at_ms,
                "lastStatus": job.state.last_status,
                "lastError": job.state.last_error,
            },
            "created_at_ms": job.created_at_ms,
            "updated_at_ms": job.updated_at_ms,
            "delete_after_run": job.delete_after_run,
        }

    def _load_store(self) -> CronStore:
        """Load jobs from disk."""
        if self._store is not None:
            return self._store

        try:
            self._migrate_legacy_json_if_needed()
            rows = self._sqlite.cron.list_all()
            self._store = CronStore(jobs=[self._cron_job_from_row(row) for row in rows])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load cron store: {}", exc)
            self._store = CronStore()

        return self._store

    def _save_store(self) -> None:
        """Save jobs to disk."""
        if self._store is None:
            return

        self._sqlite.cron.save_all([self._cron_job_to_row(job) for job in self._store.jobs])

    async def start(self) -> None:
        """Start the cron service."""
        self._running = True
        store = self._load_store()
        self._recompute_next_runs()
        self._save_store()
        self._arm_timer()
        logger.info("Cron service started with {} jobs", len(store.jobs))

    def stop(self) -> None:
        """Stop the cron service."""
        self._running = False
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None

    def _recompute_next_runs(self) -> None:
        """Recompute next run times for all enabled jobs."""
        if self._store is None:
            return
        store = self._store
        now = _now_ms()
        for job in store.jobs:
            if job.enabled:
                job.state.next_run_at_ms = _compute_next_run(job.schedule, now)

    def _get_next_wake_ms(self) -> int | None:
        """Get the earliest next run time across all jobs."""
        if self._store is None:
            return None
        store = self._store
        times = [j.state.next_run_at_ms for j in store.jobs
                 if j.enabled and j.state.next_run_at_ms]
        return min(times) if times else None

    def _arm_timer(self) -> None:
        """Schedule the next timer tick."""
        if self._timer_task:
            self._timer_task.cancel()

        next_wake = self._get_next_wake_ms()
        if not next_wake or not self._running:
            return

        delay_ms = max(0, next_wake - _now_ms())
        delay_s = delay_ms / 1000

        async def tick():
            await asyncio.sleep(delay_s)
            if self._running:
                await self._on_timer()

        self._timer_task = asyncio.create_task(tick())

    async def _on_timer(self) -> None:
        """Handle timer tick - run due jobs."""
        if self._store is None:
            return
        store = self._store

        now = _now_ms()
        due_jobs = [
            j for j in store.jobs
            if j.enabled and j.state.next_run_at_ms and now >= j.state.next_run_at_ms
        ]

        for job in due_jobs:
            await self._execute_job(job)

        self._save_store()
        self._arm_timer()

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a single job."""
        start_ms = _now_ms()
        logger.info("Cron: executing job '{}' ({})", job.name, job.id)

        try:
            if self.on_job:
                await self.on_job(job)

            job.state.last_status = "ok"
            job.state.last_error = None
            logger.info("Cron: job '{}' completed", job.name)

        except Exception as e:
            job.state.last_status = "error"
            job.state.last_error = str(e)
            logger.error("Cron: job '{}' failed: {}", job.name, e)

        job.state.last_run_at_ms = start_ms
        job.updated_at_ms = _now_ms()

        # Handle one-shot jobs
        if job.schedule.kind == "at":
            if job.delete_after_run:
                if self._store is not None:
                    self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
            else:
                job.enabled = False
                job.state.next_run_at_ms = None
        else:
            # Compute next run
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())

    # ========== Public API ==========

    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        """List all jobs."""
        store = self._load_store()
        jobs = store.jobs if include_disabled else [j for j in store.jobs if j.enabled]
        return sorted(jobs, key=lambda j: j.state.next_run_at_ms or float('inf'))

    def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        message: str,
        deliver: bool = False,
        channel: str | None = None,
        to: str | None = None,
        delete_after_run: bool = False,
    ) -> CronJob:
        """Add a new job."""
        _validate_schedule_for_add(schedule)
        store = self._load_store()
        now = _now_ms()

        job = CronJob(
            id=str(uuid.uuid4())[:8],
            name=name,
            enabled=True,
            schedule=schedule,
            payload=CronPayload(
                kind="agent_turn",
                message=message,
                deliver=deliver,
                channel=channel,
                to=to,
            ),
            state=CronJobState(next_run_at_ms=_compute_next_run(schedule, now)),
            created_at_ms=now,
            updated_at_ms=now,
            delete_after_run=delete_after_run,
        )

        store.jobs.append(job)
        self._save_store()
        self._arm_timer()

        logger.info("Cron: added job '{}' ({})", name, job.id)
        return job

    def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID."""
        store = self._load_store()
        before = len(store.jobs)
        store.jobs = [j for j in store.jobs if j.id != job_id]
        removed = len(store.jobs) < before

        if removed:
            self._save_store()
            self._arm_timer()
            logger.info("Cron: removed job {}", job_id)

        return removed

    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:
        """Enable or disable a job."""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                job.enabled = enabled
                job.updated_at_ms = _now_ms()
                if enabled:
                    job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
                else:
                    job.state.next_run_at_ms = None
                self._save_store()
                self._arm_timer()
                return job
        return None

    async def run_job(self, job_id: str, force: bool = False) -> bool:
        """Manually run a job."""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                if not force and not job.enabled:
                    return False
                await self._execute_job(job)
                self._save_store()
                self._arm_timer()
                return True
        return False

    def status(self) -> dict:
        """Get service status."""
        store = self._load_store()
        return {
            "enabled": self._running,
            "jobs": len(store.jobs),
            "next_wake_at_ms": self._get_next_wake_ms(),
        }
