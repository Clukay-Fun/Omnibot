"""Workspace-backed cache for schema-derived table profiles."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _normalized_field_snapshot(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
    payload = json.dumps(_normalized_field_snapshot(fields), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TableProfileCache:
    def __init__(self, *, workspace: Path | None = None, cache_path: Path | None = None):
        self._cache_path = cache_path or ((workspace / "memory" / "feishu" / "table_profile_cache.json") if workspace else None)
        self._entries: dict[str, dict[str, Any]] = {}
        self._latest_by_table: dict[str, str] = {}
        self._loaded = False

    @staticmethod
    def _cache_key(app_token: str, table_id: str, schema_hash: str) -> str:
        return f"{app_token}:{table_id}:{schema_hash}"

    def get(self, *, app_token: str, table_id: str, schema_hash: str) -> dict[str, Any] | None:
        self._ensure_loaded()
        payload = self._entries.get(self._cache_key(app_token, table_id, schema_hash))
        return dict(payload) if isinstance(payload, dict) else None

    def put(self, *, app_token: str, table_id: str, schema_hash: str, profile: dict[str, Any]) -> dict[str, Any]:
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
        self._ensure_loaded()
        key = self._latest_by_table.get(f"{app_token}:{table_id}")
        if not key:
            return None
        payload = self._entries.get(key)
        return dict(payload) if isinstance(payload, dict) else None

    def snapshot(self) -> dict[str, dict[str, Any]]:
        self._ensure_loaded()
        return {key: dict(value) for key, value in self._entries.items() if isinstance(value, dict)}

    def _ensure_loaded(self) -> None:
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
        if self._cache_path is None:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 2, "entries": self._entries, "latest_by_table": self._latest_by_table}
        self._cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
