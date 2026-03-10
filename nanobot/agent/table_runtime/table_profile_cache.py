"""
描述: 飞书多维表格特征画像缓存。
主要功能:
    - 提供基于本地文件系统的多维表格 Schema 摘要（画像）缓存。
    - 降低对多维表格以及大模型结构摘要化接口的频繁调用。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

#region Schema摘要及哈希工具

def _normalized_field_snapshot(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    用处: 归一化字段属性。参数 fields: 表格字段配置列表。

    功能:
        - 抓取字段名、类型和配置属性，按名称或类型排序以保证生成稳定不变的摘要字典结构。
    """
    normalized: list[dict[str, Any]] = []
    for item in fields:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "field_name": str(item.get("field_name") or item.get("name") or "").strip(),
                "type": item.get("type"),
                "property": item.get("property") if isinstance(item.get("property"), dict) else {},
            }
        )
    normalized.sort(key=lambda current: (str(current.get("field_name") or ""), str(current.get("type") or "")))
    return normalized


def schema_hash_for_fields(fields: list[dict[str, Any]]) -> str:
    """
    用处: 为表格字段生成唯一哈希标识。参数 fields: 字段列表。

    功能:
        - 将归一化后的字段配置转换为 JSON 字符串并计算其 SHA256 哈希值，用于监测表结构是否变更。
    """
    payload = json.dumps(_normalized_field_snapshot(fields), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

#endregion

#region 画像缓存管理


class TableProfileCache:
    """
    用处: 多维表格特征画像缓存管理类。主要操作缓存文件读写和键值生命周期。

    功能:
        - 将表格 Schema 对应的语义刻画与索引写入本地 Workspace 文件持久化。
    """
    def __init__(self, *, workspace: Path | None = None, cache_path: Path | None = None):
        """
        用处: 初始化缓存对象。参数 workspace: 主工作区路径，cache_path: 指定的完整缓存文件路径。

        功能:
            - 如果没有传入指定 cache_path，则默认挂载在 `workspace/memory/feishu/table_profile_cache.json`。
        """
        self._cache_path = cache_path or ((workspace / "memory" / "feishu" / "table_profile_cache.json") if workspace else None)
        self._entries: dict[str, dict[str, Any]] = {}
        self._latest_by_table: dict[str, str] = {}
        self._loaded = False

    @staticmethod
    def _cache_key(app_token: str, table_id: str, schema_hash: str) -> str:
        """
        用处: 生成特定的缓存键值。参数为应用级令牌、表格ID与哈希字符串。

        功能:
            - 拼装确保唯一的字符串键名。
        """
        return f"{app_token}:{table_id}:{schema_hash}"

    def get(self, *, app_token: str, table_id: str, schema_hash: str) -> dict[str, Any] | None:
        """
        用处: 获取指定的缓存特征画像字典。

        功能:
            - 按令牌、表ID和哈希寻找对应的特征缓存，找不到则返回 None。
        """
        self._ensure_loaded()
        payload = self._entries.get(self._cache_key(app_token, table_id, schema_hash))
        return dict(payload) if isinstance(payload, dict) else None

    def put(self, *, app_token: str, table_id: str, schema_hash: str, profile: dict[str, Any]) -> dict[str, Any]:
        """
        用处: 写入最新生成的缓存画像。参数 profile: 生成的字典结构。
        
        功能:
            - 持久化配置，更新对应的表级“最新”指针键名，并落磁盘。
        """
        self._ensure_loaded()
        key = self._cache_key(app_token, table_id, schema_hash)
        stored = dict(profile)
        stored.setdefault("app_token", app_token)
        stored.setdefault("table_id", table_id)
        stored.setdefault("schema_hash", schema_hash)
        self._entries[key] = stored
        self._latest_by_table[f"{app_token}:{table_id}"] = key
        self._flush()
        return dict(stored)

    def get_latest(self, *, app_token: str, table_id: str) -> dict[str, Any] | None:
        """
        用处: 取出该表当前可用的最新版本特征缓存。
        
        功能:
            - 对于只知表名而不知精准哈希的场景，安全回溯最晚的一次记忆特征。
        """
        self._ensure_loaded()
        key = self._latest_by_table.get(f"{app_token}:{table_id}")
        if not key:
            return None
        payload = self._entries.get(key)
        return dict(payload) if isinstance(payload, dict) else None

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """
        用处: 生成全部缓存记录快照。

        功能:
            - 返回深度拷贝的缓存实体集合。
        """
        self._ensure_loaded()
        return {key: dict(value) for key, value in self._entries.items() if isinstance(value, dict)}

    def _ensure_loaded(self) -> None:
        """
        用处: 保障文件系统记录被预加载进内存。

        功能:
            - 检查状态位并在初次调阅时从本地反序列化 JSON。
        """
        if self._loaded:
            return
        self._loaded = True
        if self._cache_path is None or not self._cache_path.exists():
            self._entries = {}
            return
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except Exception:
            self._entries = {}
            return
        if not isinstance(payload, dict):
            self._entries = {}
            return
        entries = payload.get("entries")
        self._entries = dict(entries) if isinstance(entries, dict) else {}
        latest = payload.get("latest_by_table")
        self._latest_by_table = dict(latest) if isinstance(latest, dict) else {}

    def _flush(self) -> None:
        """
        用处: 落盘刷新缓存内容。

        功能:
            - 在内存改动发生后通过原子覆写保证最新的对象记录保存至 JSON 文件。
        """
        if self._cache_path is None:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 2, "entries": self._entries, "latest_by_table": self._latest_by_table}
        self._cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

#endregion
