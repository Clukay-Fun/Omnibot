"""描述:
主要功能:
    - 管理可覆盖的飞书数据表别名与字段映射。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any

import yaml


#region 表注册表

class TableRegistry:
    """用处，参数

    功能:
        - 提供表别名解析和字段映射能力。
    """

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        builtin_path: Path | None = None,
    ):
        """用处，参数

        功能:
            - 初始化内置与工作区映射文件路径。
        """
        self._workspace = workspace
        self._builtin_path = builtin_path or Path(__file__).resolve().parents[2] / "skills" / "table_registry.yaml"
        self._workspace_path = (workspace / "skills" / "table_registry.yaml") if workspace else None
        self._tables: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._last_mtimes: tuple[int | None, int | None] = (None, None)

    def resolve_table(self, alias: str) -> dict[str, Any] | None:
        """用处，参数

        功能:
            - 按逻辑别名返回目标表配置。
        """
        self._ensure_loaded()
        key = alias.strip()
        if not key:
            return None
        raw = self._tables.get(key)
        if not isinstance(raw, dict):
            return None
        resolved = {
            "app_token": raw.get("app_token"),
            "table_id": raw.get("table_id"),
            "view_id": raw.get("view_id"),
        }
        return {k: v for k, v in resolved.items() if isinstance(v, str) and v.strip()}

    def map_field(self, alias: str, logical_name: str) -> str:
        """用处，参数

        功能:
            - 将逻辑字段名映射为真实字段名。
        """
        self._ensure_loaded()
        table = self._tables.get(alias.strip())
        if not isinstance(table, dict):
            return logical_name
        aliases_raw = table.get("field_aliases")
        if not isinstance(aliases_raw, dict):
            return logical_name
        lookup = logical_name.strip()
        if not lookup:
            return logical_name

        direct = aliases_raw.get(lookup)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        lowered = lookup.lower()
        for key, value in aliases_raw.items():
            if str(key).strip().lower() != lowered:
                continue
            if isinstance(value, str) and value.strip():
                return value.strip()
        return logical_name

    def map_fields(self, alias: str, fields: dict[str, Any]) -> dict[str, Any]:
        """用处，参数

        功能:
            - 批量映射字段字典中的键名。
        """
        mapped: dict[str, Any] = {}
        for key, value in fields.items():
            field_name = str(key)
            mapped[self.map_field(alias, field_name)] = value
        return mapped

    def map_filters(self, alias: str, filters: dict[str, Any]) -> dict[str, Any]:
        """用处，参数

        功能:
            - 递归映射过滤器中的字段名。
        """
        self._ensure_loaded()

        def _map_value(value: Any) -> Any:
            """用处，参数

            功能:
                - 递归处理字典与列表节点。
            """
            if isinstance(value, dict):
                out: dict[str, Any] = {}
                for k, v in value.items():
                    if k == "field_name" and isinstance(v, str):
                        out[k] = self.map_field(alias, v)
                    else:
                        out[k] = _map_value(v)
                return out
            if isinstance(value, list):
                return [_map_value(item) for item in value]
            return value

        mapped = _map_value(filters)
        return mapped if isinstance(mapped, dict) else dict(filters)

    def aliases(self) -> list[str]:
        """用处，参数

        功能:
            - 返回当前可用别名列表。
        """
        self._ensure_loaded()
        return sorted(self._tables.keys())

    def _ensure_loaded(self) -> None:
        """用处，参数

        功能:
            - 按文件更新时间自动刷新映射缓存。
        """
        builtin_mtime = self._safe_mtime(self._builtin_path)
        workspace_mtime = self._safe_mtime(self._workspace_path)
        current = (builtin_mtime, workspace_mtime)
        if self._loaded and current == self._last_mtimes:
            return

        merged: dict[str, dict[str, Any]] = {}
        self._merge_layer(merged, self._load_tables(self._builtin_path, strip_example_ids=True))
        self._merge_layer(merged, self._load_tables(self._workspace_path, strip_example_ids=False))

        self._tables = merged
        self._last_mtimes = current
        self._loaded = True

    @staticmethod
    def _safe_mtime(path: Path | None) -> int | None:
        """用处，参数

        功能:
            - 安全读取文件修改时间戳。
        """
        if path is None or not path.exists():
            return None
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return None

    @staticmethod
    def _merge_layer(base: dict[str, dict[str, Any]], overlay: dict[str, dict[str, Any]]) -> None:
        """用处，参数

        功能:
            - 将覆盖层合并到基础映射中。
        """
        for alias, payload in overlay.items():
            existing = base.get(alias)
            if isinstance(existing, dict):
                merged = deepcopy(existing)
                merged.update(payload)
                base[alias] = merged
            else:
                base[alias] = deepcopy(payload)

    @staticmethod
    def _looks_like_example_id(*, token: str, value: str) -> bool:
        """用处，参数

        功能:
            - 识别内置示例占位符 ID 以避免误请求线上接口。
        """
        normalized = value.strip().lower()
        if token == "app_token":
            return bool(re.fullmatch(r"app_[a-z0-9_]+_[0-9]{3,}", normalized))
        if token == "table_id":
            return bool(re.fullmatch(r"tbl_[a-z0-9_]+_[0-9]{3,}", normalized))
        return False

    @staticmethod
    def _load_tables(path: Path | None, *, strip_example_ids: bool) -> dict[str, dict[str, Any]]:
        """用处，参数

        功能:
            - 从 YAML 文件读取并规范化表配置。
        """
        if path is None or not path.exists():
            return {}
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(loaded, dict):
            return {}
        tables = loaded.get("tables")
        if not isinstance(tables, dict):
            return {}

        normalized: dict[str, dict[str, Any]] = {}
        for alias, value in tables.items():
            key = str(alias).strip()
            if not key or not isinstance(value, dict):
                continue
            record: dict[str, Any] = {}
            for token in ("app_token", "table_id", "view_id"):
                item = value.get(token)
                if isinstance(item, str) and item.strip():
                    cleaned = item.strip()
                    if strip_example_ids and TableRegistry._looks_like_example_id(token=token, value=cleaned):
                        continue
                    record[token] = cleaned
            aliases_raw = value.get("field_aliases")
            if isinstance(aliases_raw, dict):
                field_aliases: dict[str, str] = {}
                for k, v in aliases_raw.items():
                    source = str(k).strip()
                    target = str(v).strip()
                    if source and target:
                        field_aliases[source] = target
                if field_aliases:
                    record["field_aliases"] = field_aliases
            if record:
                normalized[key] = record
        return normalized


#endregion
