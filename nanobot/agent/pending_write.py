"""Helpers for structured pending-write confirmation flows."""

from __future__ import annotations

import json
import re
from typing import Any

from nanobot.bus.events import InboundMessage

PENDING_WRITE_METADATA_KEY = "pending_write"

_EXACT_CONFIRM_RE = re.compile(r"^\s*(?:确认|confirm)(?:\s+([a-zA-Z0-9]+))?\s*$", re.IGNORECASE)
_EXACT_CANCEL_RE = re.compile(r"^\s*(?:取消|cancel)(?:\s+([a-zA-Z0-9]+))?\s*$", re.IGNORECASE)


def _safe_json(value: str) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return value


def _find_token(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[a-zA-Z0-9]{4,128}", text):
            return text
        parsed = _safe_json(text)
        if parsed is not value:
            return _find_token(parsed)
        return None
    if isinstance(value, dict):
        for key in ("token", "confirm_token", "value", "id"):
            if key in value:
                found = _find_token(value.get(key))
                if found:
                    return found
        for nested in value.values():
            found = _find_token(nested)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_token(item)
            if found:
                return found
    return None


def _extract_token_from_card_action(content: str) -> str | None:
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("action_value:") or line.startswith("form_value:"):
            _, value = line.split(":", 1)
            token = _find_token(_safe_json(value.strip()))
            if token:
                return token
    return None


def extract_pending_write_command(msg: InboundMessage) -> tuple[str | None, str | None, str]:
    content = msg.content.strip()
    exact_confirm = _EXACT_CONFIRM_RE.match(content)
    if exact_confirm:
        return "confirm", exact_confirm.group(1), ""
    exact_cancel = _EXACT_CANCEL_RE.match(content)
    if exact_cancel:
        return "cancel", exact_cancel.group(1), ""

    metadata = msg.metadata or {}
    if metadata.get("msg_type") != "card_action":
        return None, None, content

    action_key = str(metadata.get("action_key") or "").lower()
    token = _extract_token_from_card_action(content)
    if "confirm" in action_key or "确认" in action_key:
        return "confirm", token, ""
    if "cancel" in action_key or "取消" in action_key:
        return "cancel", token, ""
    return None, None, content


def format_pending_write_preview(preview: dict[str, Any], *, refreshed: bool = False) -> str:
    title = "已更新写入预览，请再次确认：" if refreshed else "请确认以下写入预览："
    lines = [f"✅ {title}", ""]

    action = str(preview.get("action") or "").strip()
    if action:
        lines.append(f"- 动作：{action}")
    table_id = str(preview.get("table_id") or "").strip()
    if table_id:
        lines.append(f"- 表格：{table_id}")
    record_id = str(preview.get("record_id") or "").strip()
    if record_id:
        lines.append(f"- 记录：{record_id}")

    fields = preview.get("fields")
    if isinstance(fields, dict) and fields:
        lines.append("- 字段：")
        for key, value in fields.items():
            rendered = value
            if isinstance(value, (dict, list)):
                rendered = json.dumps(value, ensure_ascii=False)
            text = str(rendered).replace("\n", " / ")
            lines.append(f"  - {key}: {text}")

    lines.extend(["", "直接回复“确认”执行，回复“取消”放弃；如需修改，请直接说明新的写入要求。"])
    return "\n".join(lines)


def coerce_pending_write_result(payload: dict[str, Any]) -> str:
    if payload.get("success") is True:
        if payload.get("record_id"):
            return f"已完成写入，record_id: {payload['record_id']}"
        if payload.get("deleted_record_id"):
            return f"已删除记录：{payload['deleted_record_id']}"
        return "已完成写入。"
    if payload.get("error"):
        return f"写入失败：{payload['error']}"
    return json.dumps(payload, ensure_ascii=False)


def extract_json_object(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        parsed = json.loads(candidate)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None
