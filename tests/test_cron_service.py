"""描述:
主要功能:
    - 校验定时任务服务对时区参数的处理行为。
"""

import json
import sqlite3

import pytest

from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule


#region 定时服务测试


def test_add_job_rejects_unknown_timezone(tmp_path) -> None:
    """用处，参数

    功能:
        - 验证非法时区会导致添加任务失败。
    """
    service = CronService(tmp_path / "cron" / "jobs.json")

    with pytest.raises(ValueError, match="unknown timezone 'America/Vancovuer'"):
        service.add_job(
            name="tz typo",
            schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancovuer"),
            message="hello",
        )

    assert service.list_jobs(include_disabled=True) == []


def test_add_job_accepts_valid_timezone(tmp_path) -> None:
    """用处，参数

    功能:
        - 验证合法时区可正确写入任务。
    """
    service = CronService(tmp_path / "cron" / "jobs.json")

    job = service.add_job(
        name="tz ok",
        schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancouver"),
        message="hello",
    )

    assert job.schedule.tz == "America/Vancouver"


def test_cron_service_migrates_legacy_json_to_sqlite_once(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "job-1",
                        "name": "legacy",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 60000},
                        "payload": {"kind": "agent_turn", "message": "hello", "deliver": False},
                        "state": {"nextRunAtMs": 1, "lastRunAtMs": None, "lastStatus": None, "lastError": None},
                        "createdAtMs": 1,
                        "updatedAtMs": 1,
                        "deleteAfterRun": False,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    service = CronService(store_path)
    jobs = service.list_jobs(include_disabled=True)
    jobs_again = service.list_jobs(include_disabled=True)

    assert len(jobs) == 1
    assert jobs[0].id == "job-1"
    assert len(jobs_again) == 1
    assert store_path.with_name("jobs.json.migrated").exists()
    assert store_path.with_name("jobs.json.bak").exists()

    conn = sqlite3.connect(str(store_path.with_suffix(".sqlite3")))
    row_count = conn.execute("SELECT COUNT(*) FROM cron_jobs").fetchone()[0]
    conn.close()
    assert row_count == 1


def test_cron_service_persists_create_update_delete_in_sqlite(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path)

    created = service.add_job(
        name="persist me",
        schedule=CronSchedule(kind="every", every_ms=30000),
        message="hello",
    )
    assert created.id

    reopened = CronService(store_path)
    loaded = reopened.list_jobs(include_disabled=True)
    assert len(loaded) == 1
    assert loaded[0].id == created.id

    updated = reopened.enable_job(created.id, enabled=False)
    assert updated is not None
    assert updated.enabled is False

    reopened_again = CronService(store_path)
    loaded_again = reopened_again.list_jobs(include_disabled=True)
    assert len(loaded_again) == 1
    assert loaded_again[0].enabled is False

    removed = reopened_again.remove_job(created.id)
    assert removed is True
    assert reopened_again.list_jobs(include_disabled=True) == []


#endregion
