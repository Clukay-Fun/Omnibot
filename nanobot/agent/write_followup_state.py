from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

WRITE_FOLLOWUP_CONTEXTS_KEY = "recent_write_contexts"
WRITE_FOLLOWUP_CANDIDATES_KEY = "write_followup_candidates"
WRITE_FOLLOWUP_PENDING_MESSAGE_KEY = "write_followup_pending_message"
_CONTEXT_LIMIT = 3
_TTL_MINUTES = 10
_WRITE_VERBS = ("新增", "创建", "录入", "写入", "更新", "修改", "补", "填", "删", "删除")
_PROMISE_TOKENS = ("确认后", "成功后", "我会", "我现在就", "我来按", "回你", "直接执行", "继续创建", "继续更新")


def looks_like_write_request(text: str) -> bool:
    cleaned = text.strip()
    return bool(cleaned) and any(token in cleaned for token in _WRITE_VERBS)


def looks_like_write_promise(text: str) -> bool:
    cleaned = text.strip()
    return bool(cleaned) and any(token in cleaned for token in _PROMISE_TOKENS) and any(
        token in cleaned for token in ("创建", "更新", "录入", "写入", "记录链接", "字段", "继续")
    )


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def build_write_context(*, source_text: str, assistant_text: str, selected_table: dict[str, Any] | None = None, created_at: str) -> dict[str, Any] | None:
    if not looks_like_write_request(source_text) or not looks_like_write_promise(assistant_text):
        return None
    table_name = str((selected_table or {}).get("table_name") or (selected_table or {}).get("name") or "").strip()
    table_id = str((selected_table or {}).get("table_id") or "").strip()
    return {
        "source_text": source_text.strip(),
        "assistant_text": assistant_text.strip(),
        "table_name": table_name,
        "table_id": table_id,
        "created_at": created_at,
        "status": "pending_followup",
    }


def recent_write_contexts(metadata: dict[str, Any], *, now_iso: str | None = None) -> list[dict[str, Any]]:
    now_dt = _parse_iso(now_iso) or datetime.now(tz=UTC)
    value = metadata.get(WRITE_FOLLOWUP_CONTEXTS_KEY)
    if not isinstance(value, list):
        return []
    active: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "pending_followup") != "pending_followup":
            continue
        created_at = _parse_iso(item.get("created_at"))
        if created_at is None or now_dt - created_at > timedelta(minutes=_TTL_MINUTES):
            continue
        active.append(dict(item))
    return active[:_CONTEXT_LIMIT]


def push_write_context(metadata: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    updated = dict(metadata)
    current = recent_write_contexts(updated)
    dedupe_key = str(context.get("source_text") or "").strip()
    fresh = [dict(context)]
    for item in current:
        if str(item.get("source_text") or "").strip() == dedupe_key:
            continue
        fresh.append(dict(item))
        if len(fresh) >= _CONTEXT_LIMIT:
            break
    updated[WRITE_FOLLOWUP_CONTEXTS_KEY] = fresh
    return updated


def clear_write_contexts(metadata: dict[str, Any]) -> dict[str, Any]:
    updated = dict(metadata)
    updated.pop(WRITE_FOLLOWUP_CONTEXTS_KEY, None)
    updated.pop(WRITE_FOLLOWUP_CANDIDATES_KEY, None)
    updated.pop(WRITE_FOLLOWUP_PENDING_MESSAGE_KEY, None)
    return updated


def set_write_followup_candidates(metadata: dict[str, Any], *, contexts: list[dict[str, Any]], current_message: str) -> dict[str, Any]:
    updated = dict(metadata)
    updated[WRITE_FOLLOWUP_CANDIDATES_KEY] = [dict(item) for item in contexts[:_CONTEXT_LIMIT]]
    updated[WRITE_FOLLOWUP_PENDING_MESSAGE_KEY] = current_message
    return updated


def get_write_followup_candidates(metadata: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    contexts = metadata.get(WRITE_FOLLOWUP_CANDIDATES_KEY)
    current_message = str(metadata.get(WRITE_FOLLOWUP_PENDING_MESSAGE_KEY) or "").strip()
    if not isinstance(contexts, list):
        return [], current_message
    return [dict(item) for item in contexts if isinstance(item, dict)], current_message
