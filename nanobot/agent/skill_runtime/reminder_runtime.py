"""Reminder runtime with file-based persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

CalendarHook = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


class ReminderRuntime:
    def __init__(
        self,
        store_path: Path,
        *,
        now_fn: Callable[[], datetime] | None = None,
        calendar_hook: CalendarHook | None = None,
    ):
        self._store_path = store_path
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._calendar_hook = calendar_hook

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
        reminders = [item for item in self._load_all() if item.get("user_id") == user_id]
        if not include_cancelled:
            reminders = [item for item in reminders if item.get("status") == "active"]
        reminders.sort(key=lambda item: (str(item.get("due_at") or ""), str(item.get("id") or "")))
        return {"reminders": reminders}

    def cancel_reminder(self, *, user_id: str, reminder_id: str) -> dict[str, Any]:
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

    def build_daily_summary(self, *, user_id: str, date: str) -> dict[str, Any]:
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
        if not self._store_path.exists():
            return []
        try:
            payload = json.loads(self._store_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(payload, list):
            return []
        return [dict(item) for item in payload if isinstance(item, dict)]

    def _save_all(self, reminders: list[dict[str, Any]]) -> None:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        normalized = sorted(reminders, key=lambda item: str(item.get("id") or ""))
        self._store_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _next_id(reminders: list[dict[str, Any]]) -> str:
        max_id = 0
        for item in reminders:
            raw = str(item.get("id") or "")
            if raw.startswith("r") and raw[1:].isdigit():
                max_id = max(max_id, int(raw[1:]))
        return f"r{max_id + 1:06d}"
