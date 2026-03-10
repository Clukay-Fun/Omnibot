"""日期区间解析工具：将 ISO 日期字符串转换为飞书 Bitable 过滤条件。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# region [日期过滤构建]


def _iso_to_timestamp_ms(iso_str: str) -> int:
    """
    将 ISO 格式日期字符串（如 '2024-01-01' 或 '2024-01-01T08:00:00'）
    转换为 UTC 毫秒级时间戳。
    """
    # 支持纯日期和带时间的两种格式
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(iso_str, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"无法解析日期字符串: {iso_str!r}")


def build_date_filter(
    date_field: str,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any] | None:
    """
    根据日期字段和起止日期构建飞书 Bitable search API 的 filter 结构。

    返回值示例::

        {
            "conjunction": "and",
            "conditions": [
                {
                    "field_name": "创建日期",
                    "operator": "isGreaterEqual",
                    "value": [1704067200000]
                },
                ...
            ]
        }

    如果 date_from 和 date_to 均为空，则返回 None（无需过滤）。
    """
    if not date_field or (not date_from and not date_to):
        return None

    conditions: list[dict[str, Any]] = []

    if date_from:
        ts = _iso_to_timestamp_ms(date_from)
        conditions.append({
            "field_name": date_field,
            "operator": "isGreaterEqual",
            "value": [str(ts)],
        })

    if date_to:
        ts = _iso_to_timestamp_ms(date_to)
        conditions.append({
            "field_name": date_field,
            "operator": "isLessEqual",
            "value": [str(ts)],
        })

    return {
        "conjunction": "and",
        "conditions": conditions,
    }


# endregion
