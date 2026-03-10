"""字段映射与归一化工具：将飞书原始字段名重命名为用户配置的友好名称。"""

from __future__ import annotations

from typing import Any

# region [字段映射]


def apply_field_mapping(
    fields: dict[str, Any],
    mapping: dict[str, str],
) -> dict[str, Any]:
    """
    根据配置的 field_mapping 对字段进行重命名。

    映射规则：``mapping`` 的 key 为原始字段名，value 为目标名称。
    未在映射表中的字段保持原名不变。

    Args:
        fields: 原始字段字典（来自飞书 API 返回的 record.fields）。
        mapping: 字段映射表，格式为 ``{"原始字段名": "目标名称"}``。

    Returns:
        重命名后的新字段字典（不修改原始输入）。
    """
    if not mapping:
        return fields

    return {mapping.get(k, k): v for k, v in fields.items()}


# endregion
