"""描述:
主要功能:
    - 校验定时任务服务对时区参数的处理行为。
"""

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
    assert job.state.next_run_at_ms is not None


#endregion
