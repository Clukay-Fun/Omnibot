"""飞书多维表格只读工具：提供对 Bitable 数据的查询等功能。"""

import asyncio
import difflib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.agent.object_memory import (
    is_generic_recent_object_reference,
    is_recent_object_reference,
    object_kind_for_payload,
    recent_object_focus,
    resolve_recent_object_reference,
)
from nanobot.agent.table_runtime.table_registry import TableRegistry
from nanobot.agent.table_runtime.table_profile_synthesizer import TableProfileSynthesizer
from nanobot.agent.table_runtime.table_profile_cache import schema_hash_for_fields
from nanobot.agent.tools.feishu_data.cache import TTLCache
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.date_utils import build_date_filter
from nanobot.agent.tools.feishu_data.endpoints import FeishuEndpoints
from nanobot.agent.tools.feishu_data.errors import FeishuDataAPIError
from nanobot.agent.tools.feishu_data.field_utils import apply_field_mapping
from nanobot.agent.tools.feishu_data.value_normalization import normalize_amount_value, normalize_date_string, normalize_option_value
from nanobot.config.schema import FeishuDataConfig

# region [工具定义]

class BitableSearchTool(Tool):
    """
    搜索并检索飞书多维表格 (Bitable) 中的记录。
    支持按关键词、按日期范围，以及额外的过滤器进行数据提取。
    """

    def __init__(self, config: FeishuDataConfig, client: FeishuDataClient):
        self.config = config
        self.client = client
        self._fallback_scan_page_size = 50
        cache_cfg = self.config.cache
        self._mapping_cache = TTLCache[str, dict[str, Any]](
            ttl_seconds=cache_cfg.field_mapping_ttl_seconds if cache_cfg.enabled else 0,
            max_entries=cache_cfg.max_entries,
        )

    @staticmethod
    def _build_mapping_cache_key(fields: dict[str, Any], mapping: dict[str, str]) -> str:
        mapping_sig = json.dumps(mapping, sort_keys=True, ensure_ascii=False)
        fields_sig = json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)
        return f"{mapping_sig}|{fields_sig}"

    def _apply_mapping_with_cache(self, fields: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
        if not mapping:
            return fields
        key = self._build_mapping_cache_key(fields, mapping)
        cached = self._mapping_cache.get(key)
        if cached is not None:
            return cached
        mapped = apply_field_mapping(fields, mapping)
        self._mapping_cache.set(key, mapped)
        return mapped

    def _is_value_match(self, value: Any, keyword: str) -> bool:
        """通用的值匹配逻辑，支持 list[dict] (人员)、list[str] (多选)、str (单选/文本) 等。"""
        if not value or not keyword:
            return False
        kw_lower = keyword.lower()
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    if kw_lower in str(item.get("name", "")).lower():
                        return True
                elif kw_lower in str(item).lower():
                    return True
            return False
        return kw_lower in str(value).lower()

    def _record_matches_keyword(self, fields: dict[str, Any], keyword: str) -> bool:
        if not keyword:
            return True
        return any(self._is_value_match(v, keyword) for v in fields.values())

    @staticmethod
    def _is_target_not_found_error(error: FeishuDataAPIError) -> bool:
        """识别飞书返回的 NOTEXIST 类错误（常见于表不存在或未授权）。"""
        if int(getattr(error, "code", 0) or 0) == 91402:
            return True
        message = str(getattr(error, "message", "") or "").upper()
        if "NOTEXIST" in message:
            return True
        detail = getattr(error, "detail", None)
        if isinstance(detail, dict):
            detail_msg = str(detail.get("msg") or "").upper()
            return "NOTEXIST" in detail_msg
        return False

    @staticmethod
    def _normalize_filter_operator(op: str) -> str:
        normalized = op.strip().lower()
        if normalized in {"eq", "is", "="}:
            return "is"
        if normalized in {"contains", "like"}:
            return "contains"
        if normalized in {"ne", "neq", "!="}:
            return "isNot"
        if normalized in {"gt", ">"}:
            return "isGreater"
        if normalized in {"gte", ">="}:
            return "isGreaterEqual"
        if normalized in {"lt", "<"}:
            return "isLess"
        if normalized in {"lte", "<="}:
            return "isLessEqual"
        return normalized or "is"

    @classmethod
    def _build_filter_condition(cls, *, field_name: str, operator: str, value: Any) -> dict[str, Any] | None:
        if not field_name:
            return None
        if value in (None, ""):
            return None
        values = value if isinstance(value, list) else [value]
        normalized_values = [item for item in values if item not in (None, "")]
        if not normalized_values:
            return None
        return {
            "field_name": field_name,
            "operator": cls._normalize_filter_operator(operator),
            "value": normalized_values,
        }

    def _build_extra_filter(self, extra_filters: Any) -> dict[str, Any] | None:
        if not isinstance(extra_filters, dict) or not extra_filters:
            return None

        conjunction = str(extra_filters.get("conjunction") or "and").strip().lower()
        normalized_conjunction = "or" if conjunction == "or" else "and"
        conditions: list[dict[str, Any]] = []

        raw_conditions = extra_filters.get("conditions")
        if isinstance(raw_conditions, list):
            for item in raw_conditions:
                if not isinstance(item, dict):
                    continue
                field_name = str(item.get("field_name") or item.get("field") or "").strip()
                operator = str(item.get("operator") or item.get("op") or "is")
                condition = self._build_filter_condition(
                    field_name=field_name,
                    operator=operator,
                    value=item.get("value"),
                )
                if condition:
                    conditions.append(condition)
            if conditions:
                return {"conjunction": normalized_conjunction, "conditions": conditions}

        for field_name, raw_value in extra_filters.items():
            if field_name in {"conjunction", "conditions"}:
                continue
            if raw_value in (None, ""):
                continue

            if isinstance(raw_value, dict):
                operator = str(raw_value.get("operator") or raw_value.get("op") or "is")
                value = raw_value.get("value")
            else:
                operator = "contains" if isinstance(raw_value, str) else "is"
                value = raw_value

            condition = self._build_filter_condition(
                field_name=str(field_name).strip(),
                operator=operator,
                value=value,
            )
            if condition:
                conditions.append(condition)

        if not conditions:
            return None
        return {"conjunction": normalized_conjunction, "conditions": conditions}

    def _build_payload(
        self,
        *,
        view_id: str | None,
        keyword: str | None,
        searchable_fields: list[str],
        date_from: str | None,
        date_to: str | None,
        extra_filters: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if view_id:
            payload["view_id"] = view_id

        date_field = self.config.bitable.search.date_field
        date_filter = build_date_filter(date_field, date_from, date_to)
        if date_filter:
            payload["filter"] = date_filter

        if keyword and searchable_fields:
            keyword_filter = {
                "conjunction": "or",
                "conditions": [
                    {"field_name": field_name, "operator": "contains", "value": [keyword]}
                    for field_name in searchable_fields
                ],
            }
            if "filter" in payload:
                payload["filter"] = {
                    "conjunction": "and",
                    "conditions": [payload["filter"], keyword_filter],
                }
            else:
                payload["filter"] = keyword_filter

        extra_filter = self._build_extra_filter(extra_filters)
        if extra_filter:
            if "filter" in payload:
                payload["filter"] = {
                    "conjunction": "and",
                    "conditions": [payload["filter"], extra_filter],
                }
            else:
                payload["filter"] = extra_filter

        return payload

    def _normalize_records(
        self,
        *,
        items: list[dict[str, Any]],
        app_token: str,
        table_id: str,
        keyword: str | None,
        searchable_fields: list[str],
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        domain = self.config.bitable.domain
        mapping = self.config.bitable.field_mapping

        for item in items:
            raw_fields = item.get("fields", {})
            if not isinstance(raw_fields, dict):
                continue

            if keyword and not searchable_fields and not self._record_matches_keyword(raw_fields, keyword):
                continue

            rec_id = item.get("record_id")
            url = f"{domain}/base/{app_token}?table={table_id}&record={rec_id}" if domain else ""
            mapped = self._apply_mapping_with_cache(raw_fields, mapping)
            normalized.append({
                "record_id": rec_id,
                "table_id": table_id,
                "fields": mapped,
                "fields_text": {str(k): self._to_display_text(v) for k, v in mapped.items()},
                "record_url": url,
            })

        return normalized

    @classmethod
    def _to_display_text(cls, value: Any) -> str:
        if isinstance(value, list):
            items = [cls._to_display_text(item) for item in value]
            cleaned = [item for item in items if item and item != "{}" and item != "[]"]
            return " / ".join(cleaned) if cleaned else "[]"
        if isinstance(value, dict):
            for key in ("text", "name", "title", "value"):
                nested = value.get(key)
                if nested not in (None, ""):
                    return cls._to_display_text(nested)
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    async def _search_table(
        self,
        *,
        app_token: str,
        table_id: str,
        payload: dict[str, Any],
        page_size: int,
        keyword: str | None,
        searchable_fields: list[str],
    ) -> dict[str, Any]:
        path = FeishuEndpoints.bitable_records_search(app_token, table_id)
        client_side_keyword_filter = bool(keyword and not searchable_fields)
        scan_page_size = page_size
        if client_side_keyword_filter:
            scan_page_size = max(page_size, self._fallback_scan_page_size)
            if self.config.bitable.search.max_records > 0:
                scan_page_size = min(scan_page_size, self.config.bitable.search.max_records)
        params = {"page_size": scan_page_size}

        logger.info(
            f"Bitable search: app={app_token}, table={table_id}, keyword={keyword}, filter={payload.get('filter')}"
        )
        started = time.time()

        try:
            response = await self.client.request("POST", path, params=params, json_body=payload)
            items = response.get("data", {}).get("items", [])
            normalized = self._normalize_records(
                items=items,
                app_token=app_token,
                table_id=table_id,
                keyword=keyword,
                searchable_fields=searchable_fields,
            )
            duration = time.time() - started
            logger.info(f"Bitable search completed in {duration:.2f}s, table={table_id}, found {len(normalized)} items")
            records = normalized[:page_size] if client_side_keyword_filter else normalized
            total = len(normalized) if client_side_keyword_filter else response.get("data", {}).get("total", len(normalized))
            return {
                "table_id": table_id,
                "records": records,
                "total": total,
            }
        except FeishuDataAPIError as e:
            if e.code == 1254018:
                logger.warning(
                    f"Bitable search filter failed (table={table_id}): {e}. Falling back to non-filtered search."
                )
                try:
                    fallback_payload = payload.copy()
                    fallback_payload.pop("filter", None)
                    fallback_page_size = max(page_size, self._fallback_scan_page_size)
                    response = await self.client.request(
                        "POST",
                        path,
                        params={"page_size": fallback_page_size},
                        json_body=fallback_payload,
                    )
                    items = response.get("data", {}).get("items", [])
                    normalized = self._normalize_records(
                        items=items,
                        app_token=app_token,
                        table_id=table_id,
                        keyword=keyword,
                        searchable_fields=[],
                    )
                    return {
                        "table_id": table_id,
                        "records": normalized[:page_size],
                        "total": len(normalized),
                        "warning": "部分搜索字段不存在，已回退到全量匹配模式。",
                    }
                except Exception as ex:
                    logger.error(f"Fallback search failed (table={table_id}): {ex}")
                    return {"table_id": table_id, "records": [], "total": 0, "error": str(ex)}

            if self._is_target_not_found_error(e):
                hint = "目标数据表不存在或未授权，请检查 table_registry.yaml 中的 app_token/table_id 配置。"
                logger.warning(
                    "Bitable target missing (table={} app={}): {}",
                    table_id,
                    app_token,
                    e,
                )
                return {
                    "table_id": table_id,
                    "records": [],
                    "total": 0,
                    "error": hint,
                    "error_code": e.code,
                }

            logger.error(f"Bitable search API error (table={table_id}): {e}")
            return {"table_id": table_id, "records": [], "total": 0, "error": str(e)}
        except Exception as e:
            logger.error(f"Bitable search failed (table={table_id}): {e}")
            return {"table_id": table_id, "records": [], "total": 0, "error": str(e)}

    @property
    def name(self) -> str:
        return "bitable_search"

    @property
    def description(self) -> str:
        return (
            "Search for records in Feishu Bitable. "
            "IMPORTANT: If multiple records are found, ONLY provide a summary list with project IDs and titles, "
            "and include the 'record_url' for each. DO NOT expand full details for every record as it is slow to generate. "
            "Users can click the link or ask for a specific record ID for details."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Keyword to search across searchable fields."
                },
                "date_from": {
                    "type": "string",
                    "description": "Start date for filtering in ISO format, e.g., 2024-01-01."
                },
                "date_to": {
                    "type": "string",
                    "description": "End date for filtering in ISO format, e.g., 2024-12-31."
                },
                "filters": {
                    "type": "object",
                    "description": "Additional field filters (e.g., {'Status': 'Done'})."
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of records to return."
                },
                "app_token": {
                    "type": "string",
                    "description": "Optional specific Bitable App Token. Defaults to config."
                },
                "table_id": {
                    "type": "string",
                    "description": "Optional specific Table ID. Defaults to config."
                },
                "table_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional table ID list for cross-table parallel search."
                },
                "view_id": {
                    "type": "string",
                    "description": "Optional specific View ID. Defaults to config."
                }
            }
        }

    async def execute(self, **kwargs: Any) -> str:
        app_token = kwargs.get("app_token") or self.config.bitable.default_app_token
        default_table_id = kwargs.get("table_id") or self.config.bitable.default_table_id
        table_ids = kwargs.get("table_ids")
        target_tables = [tid for tid in table_ids if isinstance(tid, str) and tid] if isinstance(table_ids, list) else []
        if not target_tables and default_table_id:
            target_tables = [default_table_id]

        if not app_token or not target_tables:
            return json.dumps({
                "error": "Missing app_token or table_id. Cannot perform search without specific target.",
                "records": []
            }, ensure_ascii=False)

        limit = kwargs.get("limit") or self.config.bitable.search.default_limit
        if self.config.bitable.search.max_records > 0:
            limit = min(limit, self.config.bitable.search.max_records)

        view_id = kwargs.get("view_id") or self.config.bitable.default_view_id
        date_from = kwargs.get("date_from")
        date_to = kwargs.get("date_to")
        searchable_fields = self.config.bitable.search.searchable_fields
        keyword = kwargs.get("keyword")
        extra_filters = kwargs.get("filters")
        payload = self._build_payload(
            view_id=view_id,
            keyword=keyword,
            searchable_fields=searchable_fields,
            date_from=date_from,
            date_to=date_to,
            extra_filters=extra_filters,
        )

        tasks = [
            self._search_table(
                app_token=app_token,
                table_id=table_id,
                payload=payload,
                page_size=limit,
                keyword=keyword,
                searchable_fields=searchable_fields,
            )
            for table_id in target_tables
        ]
        table_results = await asyncio.gather(*tasks)

        if len(table_results) == 1:
            result = table_results[0]
            if result.get("error"):
                return json.dumps({"error": result["error"], "records": []}, ensure_ascii=False)
            response_payload: dict[str, Any] = {
                "records": result.get("records", []),
                "total": result.get("total", 0),
            }
            if result.get("warning"):
                response_payload["warning"] = result["warning"]
            return json.dumps(response_payload, ensure_ascii=False)

        merged_records: list[dict[str, Any]] = []
        warnings: list[str] = []
        errors: list[dict[str, Any]] = []
        table_summaries: list[dict[str, Any]] = []
        total = 0

        for result in table_results:
            table_id = result.get("table_id")
            table_total = int(result.get("total") or 0)
            total += table_total
            table_summary: dict[str, Any] = {
                "table_id": table_id,
                "total": table_total,
            }
            if result.get("error"):
                table_summary["error"] = result["error"]
                errors.append({"table_id": table_id, "error": result["error"]})
            if result.get("warning"):
                table_summary["warning"] = result["warning"]
                warnings.append(str(result["warning"]))
            table_summaries.append(table_summary)
            merged_records.extend(result.get("records", []))

        truncated = len(merged_records) > limit
        response_payload = {
            "records": merged_records[:limit],
            "total": total,
            "tables": table_summaries,
            "truncated": truncated,
        }
        if warnings:
            response_payload["warning"] = "；".join(sorted(set(warnings)))
        if errors and len(errors) == len(table_summaries):
            response_payload["error"] = "All table searches failed."

        return json.dumps(response_payload, ensure_ascii=False)


_MATCH_TEXT_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.IGNORECASE)


def _normalize_match_text(text: str) -> str:
    parts = [part.lower() for part in _MATCH_TEXT_RE.findall(str(text or ""))]
    return "".join(parts)


def _tokenize_match_text(text: str) -> list[str]:
    return [part.lower() for part in _MATCH_TEXT_RE.findall(str(text or "")) if part.strip()]


def _char_ngrams(text: str, size: int = 2) -> set[str]:
    if len(text) < size:
        return {text} if text else set()
    return {text[index:index + size] for index in range(len(text) - size + 1)}


def _score_candidate_text(query: str, target: str, *, label: str) -> tuple[float, list[str]]:
    normalized_query = _normalize_match_text(query)
    normalized_name = _normalize_match_text(target)
    if not normalized_query or not normalized_name:
        return 0.0, []

    reasons: list[str] = []
    score = difflib.SequenceMatcher(None, normalized_query, normalized_name).ratio() * 2.0
    if normalized_name in normalized_query:
        score += 1.5
        reasons.append(f"{label}_substring")
    elif normalized_query in normalized_name:
        score += 1.0
        reasons.append(f"{label}_query_substring")

    query_tokens = set(_tokenize_match_text(query))
    table_tokens = set(_tokenize_match_text(target))
    overlap = query_tokens & table_tokens
    if overlap:
        score += len(overlap) * 0.6
        reasons.append(f"{label}_token_overlap={len(overlap)}")

    ngram_query = _char_ngrams(normalized_query)
    ngram_name = _char_ngrams(normalized_name)
    if ngram_query and ngram_name:
        ngram_overlap = len(ngram_query & ngram_name) / max(1, len(ngram_query | ngram_name))
        if ngram_overlap > 0:
            score += ngram_overlap * 1.2
            reasons.append(f"{label}_ngram_overlap={ngram_overlap:.2f}")

    return round(score, 4), reasons


def _score_table_candidate(
    query: str,
    table_name: str,
    *,
    aliases: list[str] | None = None,
    purpose: str = "",
) -> tuple[float, list[str], float]:
    score, reasons = _score_candidate_text(query, table_name, label="normalized")
    base_score = score

    alias_candidates = [str(item).strip() for item in aliases or [] if str(item).strip()]
    best_alias_score = 0.0
    best_alias_reasons: list[str] = []
    for alias in alias_candidates:
        alias_score, alias_reasons = _score_candidate_text(query, alias, label="profile_alias")
        alias_score = round(alias_score + 0.4, 4) if alias_score > 0 else 0.0
        if alias_score > best_alias_score:
            best_alias_score = alias_score
            best_alias_reasons = alias_reasons
    score += best_alias_score
    reasons.extend(best_alias_reasons)

    if purpose:
        purpose_score, purpose_reasons = _score_candidate_text(query, purpose, label="profile_purpose")
        if purpose_score > 0:
            score += round(purpose_score * 0.35, 4)
            reasons.extend(purpose_reasons)

    return round(score, 4), reasons, round(base_score, 4)


def _rank_table_candidates(query: str, tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for table in tables:
        table_name = str(table.get("name") or "").strip()
        table_id = str(table.get("table_id") or "").strip()
        if not table_name or not table_id:
            continue
        profile_raw = table.get("profile")
        profile: dict[str, Any] = dict(profile_raw) if isinstance(profile_raw, dict) else {}
        aliases_raw = profile.get("aliases")
        aliases = list(aliases_raw) if isinstance(aliases_raw, list) else []
        purpose = str(profile.get("purpose_guess") or "")
        score, reasons, base_score = _score_table_candidate(query, table_name, aliases=aliases, purpose=purpose)
        if score <= 0:
            continue
        ranked.append(
            {
                "table_id": table_id,
                "name": table_name,
                "score": score,
                "base_score": base_score,
                "reasons": reasons,
                **({"profile": profile} if profile else {}),
            }
        )
    ranked.sort(key=lambda item: (-float(item.get("score") or 0.0), len(str(item.get("name") or "")), str(item.get("name") or "")))
    return ranked


class BitableListTablesTool(Tool):
    """
    列出飞书多维表格 (Bitable) App 下的所有数据表。
    返回每张数据表的 table_id 与名称。
    """

    def __init__(self, config: FeishuDataConfig, client: FeishuDataClient):
        self.config = config
        self.client = client
        cache_cfg = self.config.cache
        self._table_cache = TTLCache[str, dict[str, Any]](
            ttl_seconds=cache_cfg.table_schema_ttl_seconds if cache_cfg.enabled else 0,
            max_entries=cache_cfg.max_entries,
        )

    @property
    def name(self) -> str:
        return "bitable_list_tables"

    @property
    def description(self) -> str:
        return (
            "List tables in a Feishu Bitable app. "
            "Supports keyword filtering and compact top-N matching results."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app_token": {
                    "type": "string",
                    "description": "Bitable App Token. Defaults to config."
                },
                "keyword": {
                    "type": "string",
                    "description": "Optional table-name keyword filter.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max matched tables to return. Defaults to 10.",
                },
                "compact": {
                    "type": "boolean",
                    "description": "Return compact payload with totals and truncation markers. Defaults to true.",
                }
            }
        }

    async def execute(self, **kwargs: Any) -> str:
        app_token = kwargs.get("app_token") or self.config.bitable.default_app_token
        keyword = str(kwargs.get("keyword") or "").strip()
        raw_limit = kwargs.get("limit")
        limit = max(1, int(raw_limit)) if raw_limit not in (None, "") else 0
        compact = kwargs.get("compact")
        compact = True if compact is None else bool(compact)
        if not app_token:
            return json.dumps({
                "error": "Missing app_token. Provide it as a parameter or configure a default.",
                "tables": []
            }, ensure_ascii=False)

        cache_key = f"tables:{app_token}"
        cached = self._table_cache.get(cache_key)
        all_tables: list[dict[str, Any]]
        if cached is not None:
            all_tables = [item for item in cached.get("tables", []) if isinstance(item, dict)]
        else:
            path = FeishuEndpoints.bitable_tables(app_token)
            try:
                res = await self.client.request("GET", path)
                items = res.get("data", {}).get("items", [])
                all_tables = [
                    {"table_id": t.get("table_id"), "name": t.get("name", "")}
                    for t in items
                ]
                self._table_cache.set(cache_key, {"tables": all_tables})
            except Exception as e:
                return json.dumps({"error": str(e), "tables": []}, ensure_ascii=False)

        try:
            tables = all_tables
            if keyword:
                keyword_lower = keyword.lower()
                tables = [table for table in all_tables if keyword_lower in str(table.get("name") or "").lower()]
            matched = len(tables)
            truncated = limit > 0 and matched > limit
            if limit > 0:
                tables = tables[:limit]
            payload: dict[str, Any] = {"tables": tables}
            if compact:
                payload.update(
                    {
                        "keyword": keyword,
                        "total": len(all_tables),
                        "matched": matched,
                        "truncated": truncated,
                    }
                )
            return json.dumps(payload, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e), "tables": []}, ensure_ascii=False)


class BitableListFieldsTool(Tool):
    """列出飞书多维表格指定数据表的字段定义。"""

    def __init__(self, config: FeishuDataConfig, client: FeishuDataClient):
        self.config = config
        self.client = client
        cache_cfg = self.config.cache
        self._field_cache = TTLCache[str, dict[str, Any]](
            ttl_seconds=cache_cfg.table_schema_ttl_seconds if cache_cfg.enabled else 0,
            max_entries=cache_cfg.max_entries,
        )

    @property
    def name(self) -> str:
        return "bitable_list_fields"

    @property
    def description(self) -> str:
        return "List field schema for a Feishu Bitable table."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app_token": {
                    "type": "string",
                    "description": "Bitable App Token. Defaults to config.",
                },
                "table_id": {
                    "type": "string",
                    "description": "Table ID. Defaults to config.",
                },
                "compact": {
                    "type": "boolean",
                    "description": "Return compact field property summary. Defaults to false.",
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        app_token = kwargs.get("app_token") or self.config.bitable.default_app_token
        table_id = kwargs.get("table_id") or self.config.bitable.default_table_id
        compact = bool(kwargs.get("compact")) if kwargs.get("compact") is not None else False
        if not app_token or not table_id:
            return json.dumps(
                {
                    "error": "Missing app_token or table_id.",
                    "fields": [],
                },
                ensure_ascii=False,
            )

        cache_key = f"fields:{app_token}:{table_id}:{int(compact)}"
        cached = self._field_cache.get(cache_key)
        if cached is not None:
            return json.dumps(cached, ensure_ascii=False)

        path = FeishuEndpoints.bitable_fields(app_token, table_id)
        try:
            response = await self.client.request("GET", path)
            items = response.get("data", {}).get("items", [])
            fields = [self._serialize_field(item, compact=compact) for item in items if isinstance(item, dict)]
            payload = {
                "app_token": app_token,
                "table_id": table_id,
                "fields": fields,
                "total": len(fields),
            }
            self._field_cache.set(cache_key, payload)
            return json.dumps(payload, ensure_ascii=False)
        except Exception as e:
            return json.dumps(
                {
                    "error": str(e),
                    "app_token": app_token,
                    "table_id": table_id,
                    "fields": [],
                },
                ensure_ascii=False,
            )

    @staticmethod
    def _serialize_field(field: dict[str, Any], *, compact: bool = False) -> dict[str, Any]:
        raw_property = field.get("property")
        property_payload: dict[str, Any] = cast(dict[str, Any], raw_property) if isinstance(raw_property, dict) else {}
        property_result: dict[str, Any] = (
            BitableListFieldsTool._compact_field_property(property_payload) if compact else property_payload
        )
        return {
            "field_id": field.get("field_id") or field.get("id"),
            "field_name": field.get("field_name") or field.get("name") or "",
            "type": field.get("type"),
            "property": property_result,
        }

    @staticmethod
    def _compact_field_property(property_payload: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        if not property_payload:
            return compact

        for key in ("multiple", "formatter", "date_formatter", "time_formatter", "auto_fill"):
            value = property_payload.get(key)
            if isinstance(value, (str, int, float, bool)):
                compact[key] = value

        options = property_payload.get("options")
        if isinstance(options, list):
            option_names = []
            for item in options:
                if isinstance(item, dict):
                    name = str(item.get("name") or item.get("text") or item.get("id") or "").strip()
                    if name:
                        option_names.append(name)
                else:
                    name = str(item).strip()
                    if name:
                        option_names.append(name)
            compact["option_count"] = len(option_names)
            compact["options_preview"] = option_names[:5]

        return compact


class BitableMatchTableTool(Tool):
    """根据自然语言请求召回最可能的多维表格候选。"""

    def __init__(
        self,
        config: FeishuDataConfig,
        client: FeishuDataClient,
        workspace: Path | None = None,
        profile_synthesizer: TableProfileSynthesizer | None = None,
    ):
        self.config = config
        self._list_tables_tool = BitableListTablesTool(config, client)
        self._table_registry = TableRegistry(workspace=workspace, profile_synthesizer=profile_synthesizer)

    def _attach_profile_metadata(self, *, app_token: str, tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for item in tables:
            if not isinstance(item, dict):
                continue
            table_id = str(item.get("table_id") or "").strip()
            table_name = str(item.get("name") or "").strip()
            payload = dict(item)
            alias = self._table_registry.find_alias(app_token=app_token, table_id=table_id, table_name=table_name)
            if alias:
                profile = self._table_registry.get_latest_profile(app_token=app_token, table_id=table_id)
                if isinstance(profile, dict):
                    payload["profile"] = {
                        "alias": alias,
                        "display_name": str(profile.get("display_name") or table_name or alias).strip() or alias,
                        "aliases": [
                            str(value).strip()
                            for value in profile.get("aliases", [])
                            if isinstance(value, str) and str(value).strip()
                        ],
                        "purpose_guess": str(profile.get("purpose_guess") or "").strip(),
                        "confidence": str(profile.get("confidence") or "medium").strip() or "medium",
                        "source": str(profile.get("source") or "heuristic"),
                    }
                else:
                    metadata = self._table_registry.get_table_metadata(alias)
                    if isinstance(metadata, dict):
                        payload["profile"] = {
                            "alias": alias,
                            "display_name": str(metadata.get("display_name") or table_name or alias).strip() or alias,
                            "aliases": [
                                str(value).strip()
                                for value in metadata.get("aliases", [])
                                if isinstance(value, str) and str(value).strip()
                            ],
                            "purpose_guess": self._table_registry._purpose_guess(str(metadata.get("display_name") or table_name or alias)),
                            "source": "heuristic",
                        }
            enriched.append(payload)
        return enriched

    @property
    def name(self) -> str:
        return "bitable_match_table"

    @property
    def description(self) -> str:
        return (
            "Resolve the most likely Feishu Bitable table candidates from natural-language intent. "
            "Use this before bitable_create when the user describes a target table semantically instead of giving an exact table_id."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language description of the target table.",
                },
                "app_token": {
                    "type": "string",
                    "description": "Bitable App Token. Defaults to config.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max candidates to return. Defaults to 5.",
                },
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs: Any) -> str:
        query = str(kwargs.get("query") or "").strip()
        app_token = kwargs.get("app_token") or self.config.bitable.default_app_token
        limit = max(1, int(kwargs.get("limit") or 5))
        if not query:
            return json.dumps({"error": "Missing query.", "candidates": []}, ensure_ascii=False)

        tables_payload = json.loads(await self._list_tables_tool.execute(app_token=app_token, compact=True))
        if tables_payload.get("error"):
            return json.dumps(
                {
                    "query": query,
                    "error": tables_payload["error"],
                    "total_tables": int(tables_payload.get("total") or 0),
                    "candidates": [],
                },
                ensure_ascii=False,
            )
        tables = [item for item in tables_payload.get("tables", []) if isinstance(item, dict)]
        tables = self._attach_profile_metadata(app_token=str(app_token), tables=tables)
        ranked = _rank_table_candidates(query, tables)
        top_candidates = ranked[:limit]
        payload: dict[str, Any] = {
            "query": query,
            "total_tables": int(tables_payload.get("total") or len(tables)),
            "matched": len(ranked),
            "candidates": top_candidates,
        }
        if ranked:
            payload["best_match"] = top_candidates[0]
        return json.dumps(payload, ensure_ascii=False)


class BitablePrepareCreateTool(Tool):
    """为 bitable_create 构建非硬编码的候选表 + 紧凑字段摘要。"""

    def __init__(
        self,
        config: FeishuDataConfig,
        client: FeishuDataClient,
        workspace: Path | None = None,
        profile_synthesizer: TableProfileSynthesizer | None = None,
    ):
        self.config = config
        self._match_tool = BitableMatchTableTool(config, client, workspace=workspace, profile_synthesizer=profile_synthesizer)
        self._field_tool = BitableListFieldsTool(config, client)
        self._search_tool = BitableSearchTool(config, client)
        self._table_registry = TableRegistry(workspace=workspace, profile_synthesizer=profile_synthesizer)
        self._runtime_metadata: dict[str, Any] = {}

    def set_runtime_context(
        self,
        channel: str,
        chat_id: str,
        sender_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        _ = (channel, chat_id, sender_id)
        self._runtime_metadata = dict(metadata or {})

    def set_turn_runtime(self, runtime: Any) -> None:
        self.set_runtime_context(runtime.channel, runtime.chat_id, runtime.sender_id, runtime.metadata)

    def _reference_query_hint(self, request_text: str, table_hint: str) -> str:
        if table_hint:
            return table_hint
        if not is_generic_recent_object_reference(request_text):
            return request_text
        selected_table = self._runtime_metadata.get("recent_selected_table") if isinstance(self._runtime_metadata.get("recent_selected_table"), dict) else {}
        table_name = str(selected_table.get("name") or selected_table.get("table_name") or "").strip()
        if table_name:
            return table_name
        focus = recent_object_focus(self._runtime_metadata)
        if focus == "case":
            return "案件项目总库"
        if focus == "contract":
            return "合同管理"
        if focus == "weekly_plan":
            return "团队周工作计划表"
        return request_text

    @staticmethod
    def _field_name_for_role(profile: dict[str, Any], *roles: str) -> str | None:
        field_roles = profile.get("field_roles") if isinstance(profile.get("field_roles"), dict) else {}
        for role in roles:
            for field_name, current_role in field_roles.items():
                if str(current_role) == role:
                    return str(field_name)
        return None

    @staticmethod
    def _extract_labeled_value(text: str, labels: tuple[str, ...]) -> str | None:
        if not labels:
            return None
        pattern = "|".join(re.escape(label) for label in labels if label)
        if not pattern:
            return None
        match = re.search(rf"(?:{pattern})\s*[:：]?\s*([^，。；;\n]+)", text)
        if not match:
            return None
        value = str(match.group(1) or "").strip()
        return value or None

    @staticmethod
    def _extract_weekly_owner(text: str) -> str | None:
        for pattern in (
            r"给([^，。,:：\s]{2,20})(?:补一下|补|写|记录|新增|添加)",
            r"([^，。,:：\s]{2,20})的?(?:本周|这周|下周)(?:工作计划|周计划|周报)",
        ):
            match = re.search(pattern, text)
            if match:
                value = str(match.group(1) or "").strip()
                if value:
                    return value
        return None

    @staticmethod
    def _extract_week_token(text: str) -> str | None:
        for token in ("本周", "这周", "下周"):
            if token in text:
                return token
        match = re.search(r"第?\d+周", text)
        if match:
            return str(match.group(0)).strip()
        return None

    @staticmethod
    def _extract_content_tail(text: str) -> str | None:
        for sep in ("：", ":"):
            if sep in text:
                tail = text.split(sep, 1)[1].strip()
                if tail:
                    return tail
        return None

    @classmethod
    def _infer_draft_fields(cls, *, request_text: str, profile: dict[str, Any]) -> dict[str, Any]:
        draft_fields: dict[str, Any] = {}
        display_name = str(profile.get("display_name") or "")
        lower_name = display_name.lower()

        if any(token in lower_name for token in ("周", "weekly", "计划", "周报")):
            owner_field = cls._field_name_for_role(profile, "owner")
            week_field = cls._field_name_for_role(profile, "week")
            content_field = cls._field_name_for_role(profile, "content")
            owner = cls._extract_weekly_owner(request_text)
            week = cls._extract_week_token(request_text)
            content = cls._extract_content_tail(request_text)
            if owner_field and owner:
                draft_fields[owner_field] = owner
            if week_field and week:
                draft_fields[week_field] = week
            if content_field and content:
                draft_fields[content_field] = content
            return draft_fields

        if any(token in lower_name for token in ("合同", "contract", "agreement")):
            labeled_mapping = [
                (cls._field_name_for_role(profile, "contract_no"), ("合同编号",)),
                (cls._field_name_for_role(profile, "vendor"), ("乙方",)),
                (cls._field_name_for_role(profile, "amount"), ("合同金额", "金额")),
                (cls._field_name_for_role(profile, "expiry_date", "deadline", "time"), ("到期时间", "到期日", "到期")),
                (cls._field_name_for_role(profile, "status"), ("合同状态", "状态")),
            ]
        elif any(token in lower_name for token in ("案件", "case", "project")):
            labeled_mapping = [
                (cls._field_name_for_role(profile, "case_no"), ("案号",)),
                (cls._field_name_for_role(profile, "case_id"), ("项目ID", "项目编号")),
                (cls._field_name_for_role(profile, "client"), ("委托人", "客户")),
                (cls._field_name_for_role(profile, "owner"), ("主办律师", "负责人")),
                (cls._field_name_for_role(profile, "status"), ("案件状态", "状态")),
            ]
        else:
            labeled_mapping = []

        for field_name, labels in labeled_mapping:
            if not field_name:
                continue
            value = cls._extract_labeled_value(request_text, labels)
            if value:
                draft_fields[field_name] = value
        return draft_fields

    @staticmethod
    def _field_schema_map(fields: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        mapping: dict[str, dict[str, Any]] = {}
        for item in fields:
            if not isinstance(item, dict):
                continue
            field_name = str(item.get("field_name") or item.get("name") or "").strip()
            if field_name:
                mapping[field_name] = dict(item)
        return mapping

    @classmethod
    def _normalize_draft_fields(
        cls,
        *,
        draft_fields: dict[str, Any],
        profile: dict[str, Any],
        fields: list[dict[str, Any]],
    ) -> dict[str, Any]:
        field_roles = profile.get("field_roles") if isinstance(profile.get("field_roles"), dict) else {}
        schema_map = cls._field_schema_map(fields)
        normalized: dict[str, Any] = {}
        for field_name, value in draft_fields.items():
            role = str(field_roles.get(field_name) or "")
            field_schema_raw = schema_map.get(field_name)
            field_schema = dict(field_schema_raw) if isinstance(field_schema_raw, dict) else {}
            field_type = field_schema.get("type")
            property_payload = field_schema.get("property") if isinstance(field_schema.get("property"), dict) else {}
            options_preview_raw = property_payload.get("options_preview")
            options_preview = [
                str(item).strip()
                for item in options_preview_raw
                if str(item).strip()
            ] if isinstance(options_preview_raw, list) else []
            if role in {"time", "date", "deadline", "expiry_date", "created_at", "updated_at", "completed_at"} or field_type == 5:
                normalized[field_name] = normalize_date_string(value)
                continue
            if role == "amount" or field_type in {2}:
                amount = normalize_amount_value(value)
                normalized[field_name] = str(amount) if isinstance(amount, (int, float)) else amount
                continue
            if role == "status" or options_preview:
                normalized[field_name] = normalize_option_value(value, options_preview)
                continue
            normalized[field_name] = value
        return normalized

    def _merge_recent_object_identity(
        self,
        *,
        request_text: str,
        profile: dict[str, Any],
        draft_fields: dict[str, Any],
    ) -> dict[str, Any]:
        kind = object_kind_for_payload(profile=profile, table=None)
        if not kind:
            return draft_fields
        focus = recent_object_focus(self._runtime_metadata)
        explicit_ref = is_recent_object_reference(request_text, kind=kind)
        generic_ref = is_generic_recent_object_reference(request_text) and focus == kind
        if not explicit_ref and not generic_ref:
            return draft_fields
        resolved = resolve_recent_object_reference(self._runtime_metadata, kind=kind, text=request_text)
        if not isinstance(resolved, dict):
            return draft_fields
        identity_values = resolved.get("identity_values") if isinstance(resolved.get("identity_values"), dict) else {}
        merged = dict(draft_fields)
        for field_name, value in identity_values.items():
            if field_name not in merged and value not in (None, "", [], {}):
                merged[str(field_name)] = value
        return merged

    @staticmethod
    def _operation_from_lookup(
        *,
        draft_fields: dict[str, Any],
        identity_fields: list[str],
        profile: dict[str, Any],
        lookup_records: list[dict[str, Any]],
    ) -> tuple[str, bool, dict[str, Any]]:
        if not identity_fields:
            return "create_new", False, dict(draft_fields)
        if any(field_name not in draft_fields for field_name in identity_fields):
            return "create_new", False, dict(draft_fields)
        if not lookup_records:
            return "create_new", False, dict(draft_fields)
        if len(lookup_records) == 1:
            all_identity_fields = {
                str(item).strip()
                for strategy in (profile.get("identity_strategies") if isinstance(profile.get("identity_strategies"), list) else [])
                if isinstance(strategy, list)
                for item in strategy
                if str(item).strip()
            } or set(identity_fields)
            update_fields = {key: value for key, value in draft_fields.items() if key not in all_identity_fields}
            return "update_existing", False, update_fields
        return "ambiguous_existing", True, dict(draft_fields)

    @staticmethod
    def _select_identity_strategy(profile: dict[str, Any], draft_fields: dict[str, Any]) -> list[str]:
        strategies_raw = profile.get("identity_strategies") if isinstance(profile.get("identity_strategies"), list) else []
        strategies: list[list[str]] = []
        for strategy in strategies_raw:
            if not isinstance(strategy, list):
                continue
            cleaned = [str(item).strip() for item in strategy if str(item).strip()]
            if cleaned:
                strategies.append(cleaned)
        if not strategies:
            fallback = [
                str(item).strip()
                for item in (profile.get("identity_fields_guess") if isinstance(profile.get("identity_fields_guess"), list) else [])
                if str(item).strip()
            ]
            return fallback
        scored = sorted(
            strategies,
            key=lambda current: (
                -sum(1 for field_name in current if field_name in draft_fields),
                len(current),
            ),
        )
        return scored[0]

    async def _lookup_existing_records(
        self,
        *,
        app_token: str,
        table_id: str,
        identity_fields: list[str],
        draft_fields: dict[str, Any],
    ) -> dict[str, Any]:
        if not identity_fields or any(field_name not in draft_fields for field_name in identity_fields):
            return {
                "attempted": False,
                "matched": 0,
                "records": [],
                "filters": {field_name: draft_fields.get(field_name) for field_name in identity_fields if field_name in draft_fields},
            }
        filters = {
            "conjunction": "and",
            "conditions": [
                {"field_name": field_name, "operator": "is", "value": draft_fields[field_name]}
                for field_name in identity_fields
            ],
        }
        payload = json.loads(
            await self._search_tool.execute(app_token=app_token, table_id=table_id, filters=filters, limit=3)
        )
        records = [item for item in payload.get("records", []) if isinstance(item, dict)]
        minimal_records = [
            {
                "record_id": str(item.get("record_id") or "").strip(),
                "fields": {
                    field_name: ((item.get("fields") or {}).get(field_name) if isinstance(item.get("fields"), dict) else None)
                    for field_name in identity_fields
                },
            }
            for item in records
        ]
        return {
            "attempted": True,
            "matched": len(records),
            "records": minimal_records,
            "filters": {field_name: draft_fields.get(field_name) for field_name in identity_fields},
        }

    @property
    def name(self) -> str:
        return "bitable_prepare_create"

    @property
    def description(self) -> str:
        return (
            "Prepare a natural-language Bitable record-creation request for bitable_create. "
            "It resolves likely tables, fetches a compact create schema, and returns a dry-run-ready next step without executing the write."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_text": {
                    "type": "string",
                    "description": "Original natural-language user request for creating a record.",
                },
                "table_hint": {
                    "type": "string",
                    "description": "Optional explicit table hint if the user mentioned a likely table name.",
                },
                "app_token": {
                    "type": "string",
                    "description": "Bitable App Token. Defaults to config.",
                },
                "candidate_limit": {
                    "type": "integer",
                    "description": "How many candidate tables to consider. Defaults to 3.",
                },
            },
            "required": ["request_text"],
        }

    async def execute(self, **kwargs: Any) -> str:
        request_text = str(kwargs.get("request_text") or "").strip()
        table_hint = str(kwargs.get("table_hint") or "").strip()
        app_token = kwargs.get("app_token") or self.config.bitable.default_app_token
        candidate_limit = max(1, int(kwargs.get("candidate_limit") or 3))
        if not request_text:
            return json.dumps({"error": "Missing request_text."}, ensure_ascii=False)

        query = self._reference_query_hint(request_text, table_hint)
        matched_payload = json.loads(
            await self._match_tool.execute(query=query, app_token=app_token, limit=candidate_limit)
        )
        if matched_payload.get("error"):
            return json.dumps(
                {
                    "request_text": request_text,
                    "table_hint": table_hint,
                    "error": matched_payload["error"],
                    "needs_table_confirmation": True,
                    "candidates": [],
                },
                ensure_ascii=False,
            )
        candidates = [item for item in matched_payload.get("candidates", []) if isinstance(item, dict)]
        if not candidates:
            return json.dumps(
                {
                    "request_text": request_text,
                    "table_hint": table_hint,
                    "needs_table_confirmation": True,
                    "candidates": [],
                    "message": "No likely target table found. Ask the user which table to use.",
                },
                ensure_ascii=False,
            )

        selected = self._select_candidate(candidates)
        if selected is None:
            return json.dumps(
                {
                    "request_text": request_text,
                    "table_hint": table_hint,
                    "needs_table_confirmation": True,
                    "candidates": candidates,
                    "message": "Multiple similarly likely tables found. Ask the user to confirm which table to use.",
                },
                ensure_ascii=False,
            )

        fields_payload = json.loads(
            await self._field_tool.execute(app_token=app_token, table_id=selected["table_id"], compact=True)
        )
        if fields_payload.get("error"):
            return json.dumps(
                {
                    "request_text": request_text,
                    "table_hint": table_hint,
                    "needs_table_confirmation": False,
                    "selected_table": selected,
                    "candidates": candidates,
                    "error": fields_payload["error"],
                    "fields": [],
                },
                ensure_ascii=False,
            )
        fields = [item for item in fields_payload.get("fields", []) if isinstance(item, dict)]
        field_preview = fields[:12]
        alias = self._table_registry.find_alias(
            app_token=str(app_token),
            table_id=str(selected.get("table_id") or ""),
            table_name=str(selected.get("name") or ""),
        )
        profile = (
            await self._table_registry.get_or_synthesize_table_profile(alias, table_name=str(selected.get("name") or ""), fields=fields)
            if alias
            else None
        )
        draft_fields = (
            self._infer_draft_fields(request_text=request_text, profile=profile)
            if isinstance(profile, dict)
            else {}
        )
        if isinstance(profile, dict) and draft_fields:
            draft_fields = self._normalize_draft_fields(draft_fields=draft_fields, profile=profile, fields=fields)
            draft_fields = self._merge_recent_object_identity(request_text=request_text, profile=profile, draft_fields=draft_fields)
        elif isinstance(profile, dict):
            draft_fields = self._merge_recent_object_identity(request_text=request_text, profile=profile, draft_fields=draft_fields)
        identity_fields = self._select_identity_strategy(profile, draft_fields) if isinstance(profile, dict) else []
        missing_identity_fields = [field_name for field_name in identity_fields if field_name not in draft_fields]
        record_lookup = await self._lookup_existing_records(
            app_token=str(app_token),
            table_id=str(selected["table_id"]),
            identity_fields=identity_fields,
            draft_fields=draft_fields,
        )
        operation_guess, needs_record_confirmation, next_fields = self._operation_from_lookup(
            draft_fields=draft_fields,
            identity_fields=identity_fields,
            profile=profile if isinstance(profile, dict) else {},
            lookup_records=[item for item in record_lookup.get("records", []) if isinstance(item, dict)],
        )
        next_step = {
            "tool": "bitable_create",
            "mode": "dry_run",
            "arguments": {
                "app_token": app_token,
                "table_id": selected["table_id"],
                "fields": draft_fields,
            },
        }
        if operation_guess == "update_existing" and record_lookup.get("records"):
            next_step = {
                "tool": "bitable_update",
                "mode": "dry_run",
                "arguments": {
                    "app_token": app_token,
                    "table_id": selected["table_id"],
                    "record_id": record_lookup["records"][0]["record_id"],
                    "fields": next_fields,
                },
            }
        elif operation_guess == "ambiguous_existing":
            next_step = None
        payload = {
            "request_text": request_text,
            "table_hint": table_hint,
            "needs_table_confirmation": False,
            "selected_table": selected,
            "candidates": candidates,
            "field_total": len(fields),
            "fields": field_preview,
            "fields_truncated": len(fields) > len(field_preview),
            "suggested_field_names": [str(item.get("field_name") or "") for item in field_preview if str(item.get("field_name") or "").strip()],
            **({"profile": profile} if isinstance(profile, dict) else {}),
            "identity_strategy": identity_fields,
            "draft_fields": draft_fields,
            "missing_identity_fields": missing_identity_fields,
            "record_lookup": record_lookup,
            "operation_guess": operation_guess,
            "needs_record_confirmation": needs_record_confirmation,
            "next_step": next_step,
            "message": (
                "Multiple existing records matched; confirm the target record before writing."
                if operation_guess == "ambiguous_existing"
                else "Use the selected table and fill the fields object, then call the suggested write tool in dry_run mode."
            ),
        }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _select_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not candidates:
            return None
        top = candidates[0]
        top_score = float(top.get("score") or 0.0)
        second_score = float(candidates[1].get("score") or 0.0) if len(candidates) > 1 else 0.0
        top_base_score = float(top.get("base_score") or 0.0)
        second_base_score = float(candidates[1].get("base_score") or 0.0) if len(candidates) > 1 else 0.0
        reasons_raw = top.get("reasons") if isinstance(top.get("reasons"), list) else []
        reasons = {str(item).strip() for item in reasons_raw if str(item).strip()}
        has_strong_name_signal = any(
            reason in {
                "normalized_substring",
                "normalized_query_substring",
                "profile_alias_substring",
                "profile_alias_query_substring",
            }
            for reason in reasons
        )
        base_gap = top_base_score - second_base_score
        if len(candidates) == 1:
            return top if top_score >= 0.8 else None
        if has_strong_name_signal and top_score >= 2.4 and (top_score - second_score) >= 0.2 and base_gap >= 0.2:
            return top
        if has_strong_name_signal and top_score >= 1.8 and (top_score - second_score) >= 0.45 and base_gap >= 0.45:
            return top
        return None


class BitableSyncSchemaTool(Tool):
    """拉取多维表格 schema 快照并可落盘到 workspace。"""

    def __init__(
        self,
        config: FeishuDataConfig,
        client: FeishuDataClient,
        workspace: Path | None = None,
        profile_synthesizer: TableProfileSynthesizer | None = None,
    ):
        self.config = config
        self.client = client
        self._workspace = workspace
        self._snapshot_path = (workspace / "skills" / "table_schema_snapshot.json") if workspace else None
        self._table_registry = TableRegistry(workspace=workspace, profile_synthesizer=profile_synthesizer) if workspace else None

    @property
    def name(self) -> str:
        return "bitable_sync_schema"

    @property
    def description(self) -> str:
        return "Sync Feishu Bitable table and field schema snapshot."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app_token": {
                    "type": "string",
                    "description": "Bitable App Token. Defaults to config.",
                },
                "table_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional table IDs to sync. Defaults to all tables in app.",
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        app_token = kwargs.get("app_token") or self.config.bitable.default_app_token
        if not app_token:
            return json.dumps({"error": "Missing app_token.", "tables": []}, ensure_ascii=False)

        table_name_map: dict[str, str] = {}
        selected_table_ids = kwargs.get("table_ids")
        target_table_ids = [
            str(item).strip()
            for item in selected_table_ids
            if isinstance(item, str) and str(item).strip()
        ] if isinstance(selected_table_ids, list) else []

        try:
            table_response = await self.client.request("GET", FeishuEndpoints.bitable_tables(app_token))
            table_items = table_response.get("data", {}).get("items", [])
            for item in table_items:
                if not isinstance(item, dict):
                    continue
                table_id = str(item.get("table_id") or "").strip()
                if not table_id:
                    continue
                table_name_map[table_id] = str(item.get("name") or "")
            if not target_table_ids:
                target_table_ids = list(table_name_map.keys())
        except Exception as e:
            if not target_table_ids:
                return json.dumps({"error": str(e), "tables": []}, ensure_ascii=False)

        tables_payload: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for table_id in target_table_ids:
            try:
                response = await self.client.request("GET", FeishuEndpoints.bitable_fields(app_token, table_id))
                raw_fields = response.get("data", {}).get("items", [])
                fields = [
                    BitableListFieldsTool._serialize_field(item, compact=False)
                    for item in raw_fields
                    if isinstance(item, dict)
                ]
                table_name = table_name_map.get(table_id, "")
                schema_hash = schema_hash_for_fields(fields)
                profile = None
                if self._table_registry is not None:
                    alias = self._table_registry.find_alias(app_token=str(app_token), table_id=table_id, table_name=table_name)
                    if alias:
                        profile = await self._table_registry.get_or_synthesize_table_profile(alias, table_name=table_name, fields=fields)
                tables_payload.append(
                    {
                        "table_id": table_id,
                        "name": table_name,
                        "fields": fields,
                        "field_count": len(fields),
                        "schema_hash": schema_hash,
                        **({"profile": profile} if isinstance(profile, dict) else {}),
                    }
                )
            except Exception as e:
                errors.append({"table_id": table_id, "error": str(e)})

        payload: dict[str, Any] = {
            "app_token": app_token,
            "synced_at": datetime.now(UTC).isoformat(),
            "tables": tables_payload,
            "total_tables": len(tables_payload),
        }
        if errors:
            payload["errors"] = errors

        if self._snapshot_path is not None:
            try:
                self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
                self._snapshot_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                payload["saved_to"] = str(self._snapshot_path)
            except Exception as e:
                payload["save_error"] = str(e)

        return json.dumps(payload, ensure_ascii=False)


class BitableGetTool(Tool):
    """
    根据 record_id 获取飞书多维表格中的单条记录详情。
    """

    def __init__(self, config: FeishuDataConfig, client: FeishuDataClient):
        self.config = config
        self.client = client

    @property
    def name(self) -> str:
        return "bitable_get"

    @property
    def description(self) -> str:
        return (
            "Get a single record from Feishu Bitable by record ID. "
            "Returns the full record fields."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "record_id": {
                    "type": "string",
                    "description": "The record ID to retrieve."
                },
                "app_token": {
                    "type": "string",
                    "description": "Bitable App Token. Defaults to config."
                },
                "table_id": {
                    "type": "string",
                    "description": "Table ID. Defaults to config."
                }
            },
            "required": ["record_id"]
        }

    async def execute(self, **kwargs: Any) -> str:
        app_token = kwargs.get("app_token") or self.config.bitable.default_app_token
        table_id = kwargs.get("table_id") or self.config.bitable.default_table_id
        record_id = kwargs.get("record_id")

        if not app_token or not table_id:
            return json.dumps({
                "error": "Missing app_token or table_id.",
                "record": None
            }, ensure_ascii=False)

        if not record_id:
            return json.dumps({
                "error": "Missing record_id.",
                "record": None
            }, ensure_ascii=False)

        path = FeishuEndpoints.bitable_record(app_token, table_id, record_id)
        try:
            res = await self.client.request("GET", path)
            record = res.get("data", {}).get("record", {})
            domain = self.config.bitable.domain
            url = f"{domain}/base/{app_token}?table={table_id}&record={record_id}" if domain else ""
            mapped = apply_field_mapping(record.get("fields", {}), self.config.bitable.field_mapping)
            return json.dumps({
                "record": {
                    "record_id": record.get("record_id", record_id),
                    "fields": mapped,
                    "record_url": url,
                }
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e), "record": None}, ensure_ascii=False)


class BitableSearchPersonTool(Tool):
    """
    在飞书多维表格中按人员姓名搜索记录。
    本质是 bitable_search 的变体，显式要求 person_name 参数，
    搜索范围限定于配置中声明的可搜索人员字段。
    """

    def __init__(self, config: FeishuDataConfig, client: FeishuDataClient):
        self.config = config
        self.client = client
        cache_cfg = self.config.cache
        self._person_cache = TTLCache[str, dict[str, Any]](
            ttl_seconds=cache_cfg.person_mapping_ttl_seconds if cache_cfg.enabled else 0,
            max_entries=cache_cfg.max_entries,
        )
        self._mapping_cache = TTLCache[str, dict[str, Any]](
            ttl_seconds=cache_cfg.field_mapping_ttl_seconds if cache_cfg.enabled else 0,
            max_entries=cache_cfg.max_entries,
        )

    @staticmethod
    def _build_mapping_cache_key(fields: dict[str, Any], mapping: dict[str, str]) -> str:
        mapping_sig = json.dumps(mapping, sort_keys=True, ensure_ascii=False)
        fields_sig = json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)
        return f"{mapping_sig}|{fields_sig}"

    def _apply_mapping_with_cache(self, fields: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
        if not mapping:
            return fields
        key = self._build_mapping_cache_key(fields, mapping)
        cached = self._mapping_cache.get(key)
        if cached is not None:
            return cached
        mapped = apply_field_mapping(fields, mapping)
        self._mapping_cache.set(key, mapped)
        return mapped

    def _is_value_match(self, value: Any, keyword: str) -> bool:
        """通用的值匹配逻辑，支持人员字段、多选、单选及文本。"""
        if not value or not keyword:
            return False
        kw_lower = keyword.lower()
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    if kw_lower in str(item.get("name", "")).lower():
                        return True
                elif kw_lower in str(item).lower():
                    return True
            return False
        return kw_lower in str(value).lower()

    @property
    def name(self) -> str:
        return "bitable_search_person"

    @property
    def description(self) -> str:
        return (
            "Search records in Feishu Bitable by person name. "
            "Looks up records where a person-type field matches the given name."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "person_name": {
                    "type": "string",
                    "description": "Name of the person to search for."
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of records to return."
                },
                "app_token": {
                    "type": "string",
                    "description": "Bitable App Token. Defaults to config."
                },
                "table_id": {
                    "type": "string",
                    "description": "Table ID. Defaults to config."
                },
                "view_id": {
                    "type": "string",
                    "description": "View ID. Defaults to config."
                },
                "date_from": {
                    "type": "string",
                    "description": "Start date for filtering in ISO format, e.g., 2024-01-01."
                },
                "date_to": {
                    "type": "string",
                    "description": "End date for filtering in ISO format, e.g., 2024-12-31."
                }
            },
            "required": ["person_name"]
        }

    async def execute(self, **kwargs: Any) -> str:
        app_token = kwargs.get("app_token") or self.config.bitable.default_app_token
        table_id = kwargs.get("table_id") or self.config.bitable.default_table_id
        person_name = kwargs.get("person_name")

        if not app_token or not table_id:
            return json.dumps({
                "error": "Missing app_token or table_id.",
                "records": []
            }, ensure_ascii=False)

        if not person_name:
            return json.dumps({
                "error": "Missing person_name.",
                "records": []
            }, ensure_ascii=False)

        limit = kwargs.get("limit") or self.config.bitable.search.default_limit
        if self.config.bitable.search.max_records > 0:
            limit = min(limit, self.config.bitable.search.max_records)
        scan_page_size = max(limit, 50)
        if self.config.bitable.search.max_records > 0:
            scan_page_size = min(scan_page_size, self.config.bitable.search.max_records)

        view_id = kwargs.get("view_id") or self.config.bitable.default_view_id

        cache_key = json.dumps({
            "app_token": app_token,
            "table_id": table_id,
            "person_name": person_name,
            "limit": limit,
            "view_id": view_id,
            "date_from": kwargs.get("date_from"),
            "date_to": kwargs.get("date_to"),
        }, ensure_ascii=False, sort_keys=True)
        cached_payload = self._person_cache.get(cache_key)
        if cached_payload is not None:
            return json.dumps(cached_payload, ensure_ascii=False)

        payload: dict[str, Any] = {}
        if view_id:
            payload["view_id"] = view_id

        # 日期区间过滤
        date_from = kwargs.get("date_from")
        date_to = kwargs.get("date_to")
        date_field = self.config.bitable.search.date_field
        date_filter = build_date_filter(date_field, date_from, date_to)
        if date_filter:
            payload["filter"] = date_filter

        # 增加服务器端人员名称过滤（如果配置了 searchable_fields 且包含人员字段）
        # 注意：由于人员字段在 API 中处理较复杂，暂且尝试对所有 searchable_fields 进行包含匹配
        search_fields = self.config.bitable.search.searchable_fields
        if person_name and search_fields:
            pn_filter = {
                "conjunction": "or",
                "conditions": [
                    {"field_name": f, "operator": "contains", "value": [person_name]}
                    for f in search_fields
                ]
            }
            if "filter" in payload:
                payload["filter"] = {"conjunction": "and", "conditions": [payload["filter"], pn_filter]}
            else:
                payload["filter"] = pn_filter

        path = FeishuEndpoints.bitable_records_search(app_token, table_id)
        params = {"page_size": scan_page_size}

        logger.info(f"Bitable search_person: app={app_token}, table={table_id}, name={person_name}")
        start_time = time.time()
        try:
            res = await self.client.request("POST", path, params=params, json_body=payload)
            items = res.get("data", {}).get("items", [])
            duration = time.time() - start_time
            logger.info(f"Bitable search_person API request completed in {duration:.2f}s, fetched {len(items)} items")

            # 在客户端侧按 person_name 筛选含有匹配人员字段的记录
            matched = []
            domain = self.config.bitable.domain
            mapping = self.config.bitable.field_mapping
            for item in items:
                fields = item.get("fields", {})
                is_hit = False
                for _field_name, field_val in fields.items():
                    if self._is_value_match(field_val, person_name):
                        is_hit = True
                        break

                if is_hit:
                    rec_id = item.get("record_id")
                    url = f"{domain}/base/{app_token}?table={table_id}&record={rec_id}" if domain else ""
                    mapped = self._apply_mapping_with_cache(fields, mapping)
                    matched.append({
                        "record_id": rec_id,
                        "fields": mapped,
                        "fields_text": {str(k): str(v) for k, v in mapped.items()},
                        "record_url": url,
                    })

            response_payload = {
                "records": matched[:limit],
                "total": len(matched)
            }
            self._person_cache.set(cache_key, response_payload)
            return json.dumps(response_payload, ensure_ascii=False)
        except FeishuDataAPIError as e:
            if e.code == 1254018:
                logger.warning(f"Bitable search_person filter failed: {e}. Falling back to non-filtered search.")
                try:
                    fallback_payload = payload.copy()
                    fallback_payload.pop("filter", None)
                    res = await self.client.request("POST", path, params=params, json_body=fallback_payload)
                    items = res.get("data", {}).get("items", [])

                    matched = []
                    domain = self.config.bitable.domain
                    mapping = self.config.bitable.field_mapping
                    for item in items:
                        fields = item.get("fields", {})
                        is_hit = False
                        for _field_name, field_val in fields.items():
                            if self._is_value_match(field_val, person_name):
                                is_hit = True
                                break
                        if is_hit:
                            rec_id = item.get("record_id")
                            url = f"{domain}/base/{app_token}?table={table_id}&record={rec_id}" if domain else ""
                            mapped = self._apply_mapping_with_cache(fields, mapping)
                            matched.append({
                                "record_id": rec_id,
                                "fields": mapped,
                                "fields_text": {str(k): str(v) for k, v in mapped.items()},
                                "record_url": url,
                            })

                    response_payload = {
                        "records": matched[:limit],
                        "total": len(matched),
                        "warning": "部分人员搜索字段在表中未找到，已回退到全量扫描模式。"
                    }
                    self._person_cache.set(cache_key, response_payload)
                    return json.dumps(response_payload, ensure_ascii=False)
                except Exception as ex:
                    logger.error(f"search_person fallback failed: {ex}")
                    return json.dumps({"error": str(ex), "records": []}, ensure_ascii=False)

            logger.error(f"Bitable search_person API error: {e}")
            return json.dumps({"error": str(e), "records": []}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Bitable search_person failed: {e}")
            return json.dumps({"error": str(e), "records": []}, ensure_ascii=False)


# endregion
