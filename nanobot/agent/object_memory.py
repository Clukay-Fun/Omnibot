"""
描述: 结构化的实体对象内存管理器。
主要功能:
    - 为常驻业务对象（例如被反复引用的项目、案件、表单记录等）提供指代词与历史栈管理。
    - 处理自然语言中诸如“刚才那一条”、“第二个合同”的序列解析引用。
"""

from __future__ import annotations


import json
import re
from typing import Any

_CASE_KEY = "recent_case_objects"
_CONTRACT_KEY = "recent_contract_objects"
_WEEKLY_KEY = "recent_weekly_plan_objects"
_FOCUS_KEY = "recent_object_focus"
_HISTORY_LIMIT = 3
_ORDINAL_HINTS = {
    "第一个": 0,
    "第1个": 0,
    "第一个合同": 0,
    "第一个案件": 0,
    "第一个周计划": 0,
    "第一个记录": 0,
    "第二个": 1,
    "第2个": 1,
    "上一个": 1,
    "前一个": 1,
    "前面那个": 1,
    "不是这个": 1,
    "第三个": 2,
    "第3个": 2,
}

# region [泛实体归类与记录提取]

def object_kind_for_payload(*, profile: dict[str, Any] | None = None, table: dict[str, Any] | None = None) -> str | None:
    """
    用处: 解析表名或实体标签。

    功能:
        - 通过字符串匹配规则区分当前的查询焦点到底是 Case、Contract 还是 Weekly Plan 等预设分类。
    """
    text = " ".join(
        str(item).strip()
        for item in (
            (profile or {}).get("display_name"),
            json.dumps((profile or {}).get("aliases") or [], ensure_ascii=False),
            (table or {}).get("name"),
            (table or {}).get("table_name"),
        )
        if str(item or "").strip()
    ).lower()
    if any(token in text for token in ("案件", "case", "project")):
        return "case"
    if any(token in text for token in ("合同", "contract", "agreement")):
        return "contract"
    if any(token in text for token in ("周", "weekly", "周报", "工作计划")):
        return "weekly_plan"
    return None


def history_key_for_kind(kind: str) -> str | None:
    if kind == "case":
        return _CASE_KEY
    if kind == "contract":
        return _CONTRACT_KEY
    if kind == "weekly_plan":
        return _WEEKLY_KEY
    return None


def recent_objects(metadata: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    key = history_key_for_kind(kind)
    if not key:
        return []
    value = metadata.get(key)
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def build_object_entry(
    *,
    selected_table: dict[str, Any] | None,
    profile: dict[str, Any] | None,
    draft_fields: dict[str, Any] | None,
    identity_strategy: list[str] | None,
    record_lookup: dict[str, Any] | None,
    operation_guess: str = "",
) -> dict[str, Any] | None:
    kind = object_kind_for_payload(profile=profile, table=selected_table)
    if not kind:
        return None
    identity_fields = [str(item).strip() for item in (identity_strategy or []) if str(item).strip()]
    identity_values = {
        field_name: (draft_fields or {}).get(field_name)
        for field_name in identity_fields
        if (draft_fields or {}).get(field_name) not in (None, "", [], {})
    }
    records = [dict(item) for item in ((record_lookup or {}).get("records") or []) if isinstance(item, dict)]
    record_id = ""
    if len(records) == 1:
        record_id = str(records[0].get("record_id") or "").strip()
    if not identity_values and not record_id:
        return None
    labels = [str(value).strip() for value in identity_values.values() if str(value).strip()]
    display_label = " / ".join(labels[:3]) or str((selected_table or {}).get("name") or (profile or {}).get("display_name") or "").strip()
    return {
        "kind": kind,
        "table_id": str((selected_table or {}).get("table_id") or "").strip(),
        "table_name": str((selected_table or {}).get("name") or (profile or {}).get("display_name") or "").strip(),
        "record_id": record_id,
        "identity_strategy": identity_fields,
        "identity_values": dict(identity_values),
        "display_label": display_label,
        "operation_guess": operation_guess,
        "source": "prepare_create",
    }


def push_recent_object(metadata: dict[str, Any], entry: dict[str, Any], *, limit: int = _HISTORY_LIMIT) -> dict[str, Any]:
    kind = str(entry.get("kind") or "").strip()
    key = history_key_for_kind(kind)
    if not key:
        return metadata
    history = recent_objects(metadata, kind)
    dedupe_key = str(entry.get("record_id") or "").strip() or json.dumps(entry.get("identity_values") or {}, ensure_ascii=False, sort_keys=True)
    fresh: list[dict[str, Any]] = [dict(entry)]
    for item in history:
        item_key = str(item.get("record_id") or "").strip() or json.dumps(item.get("identity_values") or {}, ensure_ascii=False, sort_keys=True)
        if item_key == dedupe_key:
            continue
        fresh.append(dict(item))
        if len(fresh) >= limit:
            break
    updated = dict(metadata)
    updated[key] = fresh
    updated[_FOCUS_KEY] = kind
    return updated


def resolve_recent_object_reference(metadata: dict[str, Any], *, kind: str, text: str) -> dict[str, Any] | None:
    history = recent_objects(metadata, kind)
    if not history:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    index = 0
    for token, value in _ORDINAL_HINTS.items():
        if token in cleaned:
            index = value
            break
    if index >= len(history):
        return None
    return dict(history[index])


# endregion

# region [通用上下文指代判言]

def recent_object_focus(metadata: dict[str, Any]) -> str:
    return str(metadata.get(_FOCUS_KEY) or "").strip()


def is_recent_object_reference(text: str, *, kind: str) -> bool:
    lowered = text.strip()
    if not lowered:
        return False
    generic = any(token in lowered for token in ("那个", "这条", "那条", "刚才", "上一个", "前一个", "第二个", "第三个"))
    if not generic:
        return False
    if kind == "case":
        return any(token in lowered for token in ("案件", "项目"))
    if kind == "contract":
        return "合同" in lowered
    if kind == "weekly_plan":
        return any(token in lowered for token in ("周计划", "周报", "工作计划", "这周那条"))
    return False


def is_generic_recent_object_reference(text: str) -> bool:
    lowered = text.strip()
    if not lowered:
        return False
    return any(token in lowered for token in ("那个", "这条", "那条", "刚才那个", "上一个", "前一个", "不是这个", "换前一个", "换上一个"))
