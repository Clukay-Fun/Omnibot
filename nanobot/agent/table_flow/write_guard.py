from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nanobot.agent.pending_write import extract_json_object

_TABLE_PREPARE_TOOL_NAMES = frozenset({"bitable_prepare_create"})
_TABLE_WRITE_TOOL_NAMES = frozenset({"bitable_create", "bitable_update", "bitable_delete"})


@dataclass(slots=True, frozen=True)
class PreparedTableWriteFollowup:
    tool: str
    arguments: dict[str, Any]


class TableWriteGuard:
    @staticmethod
    def is_prepare_tool(tool_name: str) -> bool:
        return tool_name in _TABLE_PREPARE_TOOL_NAMES

    @staticmethod
    def is_table_write_tool(tool_name: str) -> bool:
        return tool_name in _TABLE_WRITE_TOOL_NAMES

    def extract_prepared_followup(self, *, tool_name: str, result: str) -> PreparedTableWriteFollowup | None:
        if not self.is_prepare_tool(tool_name):
            return None
        payload = extract_json_object(result)
        if not payload:
            return None
        if payload.get("needs_table_confirmation") or payload.get("needs_record_confirmation"):
            return None
        next_step = payload.get("next_step")
        if not isinstance(next_step, dict):
            return None
        followup_tool = str(next_step.get("tool") or "").strip()
        arguments = next_step.get("arguments")
        if not self.is_table_write_tool(followup_tool):
            return None
        if not isinstance(arguments, dict):
            return None
        return PreparedTableWriteFollowup(tool=followup_tool, arguments=dict(arguments))

    def build_pending_write_args(
        self,
        *,
        tool_name: str,
        raw_args: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        args = dict(raw_args)
        args.pop("confirm_token", None)
        preview_value = payload.get("preview")
        preview: dict[str, Any] = dict(preview_value) if isinstance(preview_value, dict) else {}
        if isinstance(preview.get("fields"), dict):
            args["fields"] = dict(preview["fields"])
        for key in ("table_id", "record_id"):
            value = preview.get(key)
            if value not in (None, ""):
                args[key] = value
        if tool_name == "bitable_delete":
            record_id = raw_args.get("record_id")
            if record_id not in (None, ""):
                args.setdefault("record_id", record_id)
        return args
