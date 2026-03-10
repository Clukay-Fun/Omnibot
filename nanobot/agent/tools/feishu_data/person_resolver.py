"""Resolve Feishu recipients via contact APIs with legacy directory fallback."""

from __future__ import annotations

import json
import re
from typing import Any

from nanobot.agent.tools.feishu_data.cache import TTLCache
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.endpoints import FeishuEndpoints
from nanobot.agent.tools.feishu_data.errors import FeishuDataAPIError
from nanobot.config.schema import FeishuDataConfig
from nanobot.oauth.feishu import FeishuReauthorizationRequired, FeishuUserTokenManager

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MOBILE_RE = re.compile(r"^\+?\d{6,20}$")
_CONTACT_MATCH_FIELDS = ("name", "en_name", "nickname", "email", "enterprise_email", "mobile", "open_id", "user_id")


class PersonResolutionAmbiguousError(ValueError):
    def __init__(self, candidates: list[dict[str, Any]]):
        self.candidates = candidates
        choices = ", ".join(
            f"{str(item.get('display_name') or item.get('open_id') or 'unknown')}({str(item.get('open_id') or '').strip()})"
            for item in candidates[:5]
        )
        super().__init__(f"Multiple Feishu contacts matched: {choices}")


class FeishuPersonResolver:
    def __init__(
        self,
        config: FeishuDataConfig,
        *,
        client: FeishuDataClient,
        directory: dict[str, Any] | None = None,
        user_token_manager: FeishuUserTokenManager | None = None,
    ):
        self.config = config
        self.client = client
        self.directory = dict(directory or {})
        self._user_token_manager = user_token_manager
        cache_cfg = config.cache
        self._cache = TTLCache[str, str](
            ttl_seconds=cache_cfg.person_mapping_ttl_seconds if cache_cfg.enabled else 0,
            max_entries=cache_cfg.max_entries,
        )

    @staticmethod
    def _normalize_value(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("open_id", "user_id", "id", "email", "mobile", "name", "text"):
                current = str(value.get(key) or "").strip()
                if current:
                    return current
            return ""
        return str(value or "").strip()

    @classmethod
    def _field_matches(cls, field_value: Any, lookup: str) -> bool:
        target = lookup.strip().lower()
        if not target:
            return False
        if isinstance(field_value, list):
            return any(cls._field_matches(item, lookup) for item in field_value)
        if isinstance(field_value, dict):
            return any(cls._field_matches(field_value.get(key), lookup) for key in field_value)
        return target in str(field_value or "").strip().lower()

    def _directory_settings(self) -> tuple[str, str, str, list[str]]:
        app_token = str(self.directory.get("app_token") or "").strip()
        table_id = str(self.directory.get("table_id") or "").strip()
        open_id_field = str(self.directory.get("open_id_field") or "open_id").strip()
        lookup_fields = [str(item).strip() for item in self.directory.get("lookup_fields", []) if str(item).strip()]
        return app_token, table_id, open_id_field, lookup_fields

    @staticmethod
    def _contact_display_name(payload: dict[str, Any]) -> str:
        for key in ("display_name", "name", "en_name", "nickname", "email", "mobile", "open_id"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        return ""

    @classmethod
    def _directory_display_name(cls, fields: dict[str, Any], lookup_fields: list[str], open_id_field: str) -> str:
        for key in [*lookup_fields, open_id_field, "姓名", "name", "Name"]:
            value = cls._normalize_value(fields.get(key))
            if value:
                return value
        return ""

    @staticmethod
    def _dedupe_contacts(contacts: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for item in contacts:
            if not isinstance(item, dict):
                continue
            open_id = str(item.get("open_id") or "").strip()
            key = open_id or json.dumps(item, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
            if len(deduped) >= limit:
                break
        return deduped

    @classmethod
    def _serialize_contact_item(cls, item: dict[str, Any]) -> dict[str, Any] | None:
        raw = item.get("fields") if isinstance(item.get("fields"), dict) else item
        if not isinstance(raw, dict):
            return None
        open_id = cls._normalize_value(raw.get("open_id") or raw.get("user_id") or raw.get("OpenID") or raw.get("id"))
        display_name = cls._contact_display_name(raw) or cls._normalize_value(raw.get("姓名") or raw.get("Name"))
        matched: dict[str, str] = {}
        for key in (*_CONTACT_MATCH_FIELDS, "姓名", "邮箱", "手机号", "手机"):
            value = cls._normalize_value(raw.get(key))
            if value:
                matched[key] = value
        payload = {
            "open_id": open_id,
            "display_name": display_name,
            "matched": matched,
        }
        if payload["open_id"] or payload["display_name"]:
            return payload
        return None

    @staticmethod
    def _extract_items(response: dict[str, Any]) -> list[dict[str, Any]]:
        data = response.get("data") if isinstance(response, dict) else {}
        if not isinstance(data, dict):
            return []
        for key in ("items", "user_list", "users", "user_infos"):
            items = data.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []

    async def _search_contact_api(
        self,
        lookup: str,
        *,
        limit: int,
        auth_mode: str,
        bearer_token: str | None = None,
    ) -> list[dict[str, Any]]:
        if _EMAIL_RE.match(lookup):
            response = await self.client.request(
                "POST",
                FeishuEndpoints.contact_users_batch_get_id(),
                params={"user_id_type": "open_id"},
                json_body={"emails": [lookup]},
                auth_mode=auth_mode,
                bearer_token=bearer_token,
            )
            contacts = [self._serialize_contact_item(item) for item in self._extract_items(response)]
            return self._dedupe_contacts([item for item in contacts if isinstance(item, dict)], limit=limit)

        if _MOBILE_RE.match(lookup):
            response = await self.client.request(
                "POST",
                FeishuEndpoints.contact_users_batch_get_id(),
                params={"user_id_type": "open_id"},
                json_body={"mobiles": [lookup]},
                auth_mode=auth_mode,
                bearer_token=bearer_token,
            )
            contacts = [self._serialize_contact_item(item) for item in self._extract_items(response)]
            return self._dedupe_contacts([item for item in contacts if isinstance(item, dict)], limit=limit)

        page_size = min(max(1, int(limit or 10)), max(1, int(self.config.bitable.search.max_records or 100)))
        page_token: str | None = None
        collected: list[dict[str, Any]] = []
        for _ in range(5):
            params: dict[str, Any] = {
                "department_id": "0",
                "department_id_type": "department_id",
                "user_id_type": "open_id",
                "page_size": page_size,
                "fetch_child": True,
            }
            if page_token:
                params["page_token"] = page_token
            response = await self.client.request(
                "GET",
                FeishuEndpoints.contact_users_find_by_department(),
                params=params,
                auth_mode=auth_mode,
                bearer_token=bearer_token,
            )
            for item in self._extract_items(response):
                serialized = self._serialize_contact_item(item)
                if not isinstance(serialized, dict):
                    continue
                haystack = {
                    **serialized.get("matched", {}),
                    "display_name": serialized.get("display_name"),
                    "open_id": serialized.get("open_id"),
                }
                if lookup and not self._field_matches(haystack, lookup):
                    continue
                collected.append(serialized)
                if len(self._dedupe_contacts(collected, limit=limit)) >= limit:
                    return self._dedupe_contacts(collected, limit=limit)
            data = response.get("data") if isinstance(response, dict) else {}
            if not isinstance(data, dict) or not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "").strip() or None
            if not page_token:
                break
        return self._dedupe_contacts(collected, limit=limit)

    async def _search_directory_bitable(self, lookup: str, *, limit: int) -> list[dict[str, Any]]:
        app_token, table_id, open_id_field, lookup_fields = self._directory_settings()
        if not app_token or not table_id or not lookup_fields:
            return []

        page_size = max(1, int(limit or 10))
        if self.config.bitable.search.max_records > 0:
            page_size = min(page_size, self.config.bitable.search.max_records)

        json_body: dict[str, Any] = {}
        if lookup:
            json_body = {
                "filter": {
                    "conjunction": "or",
                    "conditions": [
                        {"field_name": field_name, "operator": "contains", "value": [lookup]}
                        for field_name in lookup_fields
                    ],
                }
            }

        response = await self.client.request(
            "POST",
            FeishuEndpoints.bitable_records_search(app_token, table_id),
            params={"page_size": page_size},
            json_body=json_body,
        )

        contacts: list[dict[str, Any]] = []
        for item in self._extract_items(response):
            fields = item.get("fields", {}) if isinstance(item, dict) else {}
            if not isinstance(fields, dict):
                continue
            if lookup and not any(self._field_matches(fields.get(field_name), lookup) for field_name in lookup_fields):
                continue
            matched = {
                field_name: self._normalize_value(fields.get(field_name))
                for field_name in lookup_fields
                if self._normalize_value(fields.get(field_name))
            }
            open_id = self._normalize_value(fields.get(open_id_field))
            contacts.append(
                {
                    "open_id": open_id,
                    "display_name": self._directory_display_name(fields, lookup_fields, open_id_field),
                    "matched": matched,
                }
            )
            if len(contacts) >= page_size:
                break
        return contacts

    async def search(self, keyword: Any = None, *, limit: int = 10, actor_open_id: str | None = None) -> list[dict[str, Any]]:
        page_size = max(1, int(limit or 10))
        lookup = self._normalize_value(keyword)

        app_error: Exception | None = None
        try:
            contacts = await self._search_contact_api(lookup, limit=page_size, auth_mode="app")
        except Exception as exc:
            contacts = []
            app_error = exc
        if contacts:
            return contacts

        if actor_open_id and self._user_token_manager is not None:
            try:
                token = self._user_token_manager.get_valid_access_token(actor_open_id)
                contacts = await self._search_contact_api(
                    lookup,
                    limit=page_size,
                    auth_mode="user",
                    bearer_token=token,
                )
                if contacts:
                    return contacts
            except FeishuReauthorizationRequired:
                pass
            except Exception as exc:
                if app_error is None:
                    app_error = exc

        directory_contacts = await self._search_directory_bitable(lookup, limit=page_size)
        if directory_contacts:
            return self._dedupe_contacts(directory_contacts, limit=page_size)

        if isinstance(app_error, FeishuDataAPIError):
            return []
        return []

    async def resolve(self, value: Any, *, actor_open_id: str | None = None) -> str | None:
        lookup = self._normalize_value(value)
        if not lookup:
            return None
        if lookup.startswith("ou_"):
            return lookup

        cached = self._cache.get(lookup)
        if cached:
            return cached

        contacts = await self.search(
            keyword=lookup,
            limit=max(1, int(self.config.bitable.search.max_records or 100)),
            actor_open_id=actor_open_id,
        )
        if not contacts:
            return None
        deduped = self._dedupe_contacts(contacts, limit=max(1, len(contacts)))
        if len(deduped) > 1:
            raise PersonResolutionAmbiguousError(deduped)
        resolved = str(deduped[0].get("open_id") or "").strip()
        if resolved:
            self._cache.set(lookup, resolved)
            return resolved
        return None

    def cache_snapshot(self) -> dict[str, Any]:
        return {
            "directory": json.loads(json.dumps(self.directory, ensure_ascii=False)),
            "oauth_fallback_enabled": self._user_token_manager is not None,
        }


BitablePersonResolver = FeishuPersonResolver
