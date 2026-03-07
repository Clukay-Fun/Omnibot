"""Resolve Feishu recipients from a configured bitable directory."""

from __future__ import annotations

import json
from typing import Any

from nanobot.agent.tools.feishu_data.cache import TTLCache
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.endpoints import FeishuEndpoints
from nanobot.config.schema import FeishuDataConfig


class BitablePersonResolver:
    def __init__(
        self,
        config: FeishuDataConfig,
        *,
        client: FeishuDataClient,
        directory: dict[str, Any] | None = None,
    ):
        self.config = config
        self.client = client
        self.directory = dict(directory or {})
        cache_cfg = config.cache
        self._cache = TTLCache[str, str](
            ttl_seconds=cache_cfg.person_mapping_ttl_seconds if cache_cfg.enabled else 0,
            max_entries=cache_cfg.max_entries,
        )

    @staticmethod
    def _normalize_value(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("open_id", "id", "email", "name", "text"):
                current = str(value.get(key) or "").strip()
                if current:
                    return current
            return ""
        return str(value or "").strip()

    @staticmethod
    def _field_matches(field_value: Any, lookup: str) -> bool:
        target = lookup.strip().lower()
        if not target:
            return False
        if isinstance(field_value, list):
            return any(BitablePersonResolver._field_matches(item, lookup) for item in field_value)
        if isinstance(field_value, dict):
            return any(BitablePersonResolver._field_matches(field_value.get(key), lookup) for key in field_value)
        return target in str(field_value or "").strip().lower()

    async def resolve(self, value: Any) -> str | None:
        lookup = self._normalize_value(value)
        if not lookup:
            return None
        if lookup.startswith("ou_"):
            return lookup

        cached = self._cache.get(lookup)
        if cached:
            return cached

        app_token = str(self.directory.get("app_token") or "").strip()
        table_id = str(self.directory.get("table_id") or "").strip()
        open_id_field = str(self.directory.get("open_id_field") or "open_id").strip()
        lookup_fields = [str(item).strip() for item in self.directory.get("lookup_fields", []) if str(item).strip()]
        if not app_token or not table_id or not lookup_fields:
            return None

        response = await self.client.request(
            "POST",
            FeishuEndpoints.bitable_records_search(app_token, table_id),
            params={"page_size": self.config.bitable.search.max_records or 100},
            json_body={
                "filter": {
                    "conjunction": "or",
                    "conditions": [
                        {"field_name": field_name, "operator": "contains", "value": [lookup]}
                        for field_name in lookup_fields
                    ],
                }
            },
        )
        items = response.get("data", {}).get("items", [])
        for item in items:
            fields = item.get("fields", {}) if isinstance(item, dict) else {}
            if not any(self._field_matches(fields.get(field_name), lookup) for field_name in lookup_fields):
                continue
            resolved = self._normalize_value(fields.get(open_id_field))
            if resolved:
                self._cache.set(lookup, resolved)
                return resolved
        return None

    def cache_snapshot(self) -> dict[str, Any]:
        return {
            "directory": json.loads(json.dumps(self.directory, ensure_ascii=False)),
        }
