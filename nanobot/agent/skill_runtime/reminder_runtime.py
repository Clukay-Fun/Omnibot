"""描述:
主要功能:
    - 管理提醒项的创建、查询、取消与汇总。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from nanobot.storage.sqlite_store import SQLiteStore

CalendarHook = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


#region 提醒管理组件实现

class ReminderRuntime:
    """
    用处: 执行提醒事务记录控制及回调调度的运行期核心类。

    功能:
        - 将事件持久化至硬盘，承接外部新增与清理命令调度外部日历同步回调接口动作。
    """

    def __init__(
        self,
        store_path: Path,
        *,
        now_fn: Callable[[], datetime] | None = None,
        calendar_hook: CalendarHook | None = None,
    ):
        """
        用处: 建构基础文件关联器与依赖指针注入。参数 store_path: 落地数据源址。

        功能:
            - 设置运行时必要时钟抓取柄和相关联的日历挂载操作处理钩。
        """
        self._store_path = store_path
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._calendar_hook = calendar_hook
        self._sqlite = SQLiteStore(self._store_path.with_suffix(".sqlite3"))

    def _migrate_legacy_json_if_needed(self) -> None:
        if not self._store_path.exists():
            return

        marker_path = self._store_path.with_name(f"{self._store_path.name}.migrated")
        if marker_path.exists():
            return

        try:
            payload = json.loads(self._store_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        if not isinstance(payload, list):
            return

        for item in payload:
            if not isinstance(item, dict):
                continue
            reminder_id = str(item.get("id") or "")
            if not reminder_id:
                continue
            self._sqlite.upsert_reminder(
                {
                    "id": reminder_id,
                    "external_key": item.get("external_key"),
                    "user_id": str(item.get("user_id") or ""),
                    "chat_id": str(item.get("chat_id") or ""),
                    "channel": item.get("channel"),
                    "text": str(item.get("text") or ""),
                    "due_at": str(item.get("due_at") or ""),
                    "status": str(item.get("status") or "active"),
                    "created_at": str(item.get("created_at") or self._now_fn().isoformat()),
                    "updated_at": item.get("updated_at"),
                    "cancelled_at": item.get("cancelled_at"),
                    "calendar_event_id": item.get("calendar_event_id"),
                }
            )

        self._sqlite.maybe_backup_file(self._store_path)
        marker_path.write_text(self._now_fn().isoformat(), encoding="utf-8")

    async def create_reminder(
        self,
        *,
        user_id: str,
        chat_id: str,
        text: str,
        due_at: str,
        channel: str,
        calendar_requested: bool = False,
    ) -> dict[str, Any]:
        """
        用处: 初始化新增一枚定时的待提醒事件。

        功能:
            - 组装包含发起人在内在的基础信息进入全记录列表。
            - 若需要关联日程，激活对应钩子并将结果与原信息绑定打包发还前端呈现反馈。
        """
        reminders = self._load_all()
        reminder = {
            "id": self._next_id(reminders),
            "user_id": user_id,
            "chat_id": chat_id,
            "channel": channel,
            "text": text,
            "due_at": due_at,
            "status": "active",
            "created_at": self._now_fn().isoformat(),
        }
        reminders.append(reminder)

        calendar_payload = {"status": "skipped"}
        if calendar_requested:
            if self._calendar_hook is None:
                calendar_payload = {"status": "unavailable"}
            else:
                try:
                    event = await self._calendar_hook(reminder)
                    if isinstance(event, dict) and event.get("event_id"):
                        reminder["calendar_event_id"] = str(event["event_id"])
                    calendar_payload = {"status": "created", "event": event or {}}
                except Exception as exc:  # noqa: BLE001
                    calendar_payload = {"status": "failed", "message": str(exc)}

        self._save_all(reminders)
        return {"reminder": reminder, "calendar": calendar_payload}

    def list_reminders(self, *, user_id: str, include_cancelled: bool = False) -> dict[str, Any]:
        """
        用处: 分解呈现目标账户名下的有效待办备忘项。参数 user_id: 账户源。

        功能:
            - 按时域对符合提取前提的清单做排定排序然后打包下发。
        """
        reminders = [item for item in self._load_all() if item.get("user_id") == user_id]
        if not include_cancelled:
            reminders = [item for item in reminders if item.get("status") == "active"]
        reminders.sort(key=lambda item: (str(item.get("due_at") or ""), str(item.get("id") or "")))
        return {"reminders": reminders}

    def upsert_reminder(
        self,
        *,
        external_key: str,
        user_id: str,
        chat_id: str,
        text: str,
        due_at: str,
        channel: str,
        overwrite: bool = True,
    ) -> dict[str, Any]:
        """Create or update a reminder bound to an external integration key."""
        reminders = self._load_all()
        existing = next((item for item in reminders if item.get("external_key") == external_key), None)
        now = self._now_fn().isoformat()
        if existing is not None:
            if overwrite:
                existing.update({
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "channel": channel,
                    "text": text,
                    "due_at": due_at,
                    "status": "active",
                    "updated_at": now,
                })
                self._save_all(reminders)
                return {"created": False, "reminder": existing}
            return {"created": False, "reminder": existing}

        reminder = {
            "id": self._next_id(reminders),
            "external_key": external_key,
            "user_id": user_id,
            "chat_id": chat_id,
            "channel": channel,
            "text": text,
            "due_at": due_at,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        reminders.append(reminder)
        self._save_all(reminders)
        return {"created": True, "reminder": reminder}

    def cancel_reminder(self, *, user_id: str, reminder_id: str) -> dict[str, Any]:
        """
        用处: 根据指示定点打断或停用一项现存备忘安排。参数 reminder_id: 操作的单一指针键。

        功能:
            - 复查账户归属并把处于生机状态的项目打上取消休眠标签。
        """
        reminders = self._load_all()
        for item in reminders:
            if item.get("id") != reminder_id or item.get("user_id") != user_id:
                continue
            if item.get("status") != "active":
                return {"cancelled": False, "reason": "already_inactive", "reminder": item}
            item["status"] = "cancelled"
            item["cancelled_at"] = self._now_fn().isoformat()
            self._save_all(reminders)
            return {"cancelled": True, "reminder": item}
        return {"cancelled": False, "reason": "not_found", "reminder_id": reminder_id}

    def cancel_by_external_key(self, *, external_key: str) -> dict[str, Any]:
        """Cancel a reminder via its external integration key."""
        reminders = self._load_all()
        for item in reminders:
            if item.get("external_key") != external_key:
                continue
            if item.get("status") != "active":
                return {"cancelled": False, "reason": "already_inactive", "reminder": item}
            item["status"] = "cancelled"
            item["cancelled_at"] = self._now_fn().isoformat()
            self._save_all(reminders)
            return {"cancelled": True, "reminder": item}
        return {"cancelled": False, "reason": "not_found", "external_key": external_key}

    def build_daily_summary(self, *, user_id: str, date: str) -> dict[str, Any]:
        """
        用处: 拼接当日提醒概要报表面版内容供摘要汇报。

        功能:
            - 透视并过滤指定日期的条目量和具体分布情况，组建统计面板形态。
        """
        reminders = self.list_reminders(user_id=user_id).get("reminders", [])
        due_today = [item for item in reminders if str(item.get("due_at") or "").startswith(date)]
        return {
            "date": date,
            "user_id": user_id,
            "active_count": len(reminders),
            "due_today_count": len(due_today),
            "due_today": due_today,
        }

    def _load_all(self) -> list[dict[str, Any]]:
        """
        用处: 安全载入 JSON 长储的所有源记录。

        功能:
            - 防暴解析，对断链或受损文段主动抹平返回安全序列。
        """
        self._migrate_legacy_json_if_needed()
        return self._sqlite.list_reminders()

    def _save_all(self, reminders: list[dict[str, Any]]) -> None:
        """
        用处: 覆写全体现存记录数据池至储存磁盘。

        功能:
            - 针对 ID 特征对散乱的数据施加稳定排布再回灌写入。
        """
        normalized = sorted(reminders, key=lambda item: str(item.get("id") or ""))
        self._sqlite.save_reminders(normalized)

    @staticmethod
    def _next_id(reminders: list[dict[str, Any]]) -> str:
        """
        用处: 分发无碰撞序列的下一个单号代号。

        功能:
            - 在获取已有上限值基础之上计算并提供标准化自增前缀。
        """
        max_id = 0
        for item in reminders:
            raw = str(item.get("id") or "")
            if raw.startswith("r") and raw[1:].isdigit():
                max_id = max(max_id, int(raw[1:]))
        return f"r{max_id + 1:06d}"

#endregion
