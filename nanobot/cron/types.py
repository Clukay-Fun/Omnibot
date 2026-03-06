"""描述:
主要功能:
    - 定义定时任务调度的数据结构。
"""

from dataclasses import dataclass, field
from typing import Literal


#region 定时任务类型

@dataclass
class CronSchedule:
    """用处，参数

    功能:
        - 描述任务触发方式与时间规则。
    """
    kind: Literal["at", "every", "cron"]
    # For "at": timestamp in ms
    at_ms: int | None = None
    # For "every": interval in ms
    every_ms: int | None = None
    # For "cron": cron expression (e.g. "0 9 * * *")
    expr: str | None = None
    # Timezone for cron expressions
    tz: str | None = None


@dataclass
class CronPayload:
    """用处，参数

    功能:
        - 描述任务触发后的执行内容与投递目标。
    """
    kind: Literal["system_event", "agent_turn"] = "agent_turn"
    message: str = ""
    # Deliver response to channel
    deliver: bool = False
    channel: str | None = None  # e.g. "whatsapp"
    to: str | None = None  # e.g. phone number


@dataclass
class CronJobState:
    """用处，参数

    功能:
        - 保存任务运行时状态与最近结果。
    """
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None


@dataclass
class CronJob:
    """用处，参数

    功能:
        - 表示完整的单个定时任务对象。
    """
    id: str
    name: str
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    delete_after_run: bool = False


@dataclass
class CronStore:
    """用处，参数

    功能:
        - 表示定时任务持久化存储结构。
    """
    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)


#endregion
