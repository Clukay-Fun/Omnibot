"""飞书多维表格只读工具：提供对 Bitable 数据的查询等功能。"""

import json
import time
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.date_utils import build_date_filter
from nanobot.agent.tools.feishu_data.endpoints import FeishuEndpoints
from nanobot.agent.tools.feishu_data.errors import FeishuDataAPIError
from nanobot.agent.tools.feishu_data.field_utils import apply_field_mapping
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
                "view_id": {
                    "type": "string",
                    "description": "Optional specific View ID. Defaults to config."
                }
            }
        }

    async def execute(self, **kwargs: Any) -> str:
        app_token = kwargs.get("app_token") or self.config.bitable.default_app_token
        table_id = kwargs.get("table_id") or self.config.bitable.default_table_id

        if not app_token or not table_id:
            return json.dumps({
                "error": "Missing app_token or table_id. Cannot perform search without specific target.",
                "records": []
            }, ensure_ascii=False)

        limit = kwargs.get("limit") or self.config.bitable.search.default_limit
        if self.config.bitable.search.max_records > 0:
            limit = min(limit, self.config.bitable.search.max_records)

        view_id = kwargs.get("view_id") or self.config.bitable.default_view_id

        # 组装 payload
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

        path = FeishuEndpoints.bitable_records_search(app_token, table_id)
        params = {"page_size": limit}

        # 构造服务器端过滤器
        searchable_fields = self.config.bitable.search.searchable_fields
        keyword = kwargs.get("keyword")
        
        # 如果有关键字且有可搜索字段，构造并集过滤器
        if keyword and searchable_fields:
            kw_filter = {
                "conjunction": "or",
                "conditions": [
                    {"field_name": f, "operator": "contains", "value": [keyword]}
                    for f in searchable_fields
                ]
            }
            if "filter" in payload:
                # 合并现有的日期过滤器
                payload["filter"] = {
                    "conjunction": "and",
                    "conditions": [payload["filter"], kw_filter]
                }
            else:
                payload["filter"] = kw_filter

        logger.info(f"Bitable search: app={app_token}, table={table_id}, keyword={keyword}, filter={payload.get('filter')}")
        start_time = time.time()
        try:
            res = await self.client.request("POST", path, params=params, json_body=payload)
            items = res.get("data", {}).get("items", [])
            duration = time.time() - start_time
            logger.info(f"Bitable search completed in {duration:.2f}s, found {len(items)} items")
            
            normalized = []
            domain = self.config.bitable.domain
            mapping = self.config.bitable.field_mapping
            
            for item in items:
                rec_id = item.get("record_id")
                url = f"{domain}/base/{app_token}?table={table_id}&record={rec_id}" if domain else ""
                raw_fields = item.get("fields", {})
                
                # 如果没有服务器端过滤（或补充过滤），在客户端进行二次匹配校验
                if keyword and not searchable_fields:
                    match = False
                    for val in raw_fields.values():
                        if self._is_value_match(val, keyword):
                            match = True
                            break
                    if not match:
                        continue

                mapped = apply_field_mapping(raw_fields, mapping)
                normalized.append({
                    "record_id": rec_id,
                    "fields": mapped,
                    "fields_text": {str(k): str(v) for k, v in mapped.items()},
                    "record_url": url,
                })

            return json.dumps({
                "records": normalized,
                "total": res.get("data", {}).get("total", len(normalized))
            }, ensure_ascii=False)

        except FeishuDataAPIError as e:
            # 飞书错误码 1254018 表示过滤器字段不存在 (InvalidFilter)
            if e.code == 1254018:
                logger.warning(f"Bitable search filter failed (likely missing fields in config): {e}. Falling back to non-filtered search.")
                #  fallback: 去掉 filter 重新请求，完全依赖客户端侧 _is_value_match
                try:
                    fallback_payload = payload.copy()
                    fallback_payload.pop("filter", None)
                    res = await self.client.request("POST", path, params=params, json_body=fallback_payload)
                    items = res.get("data", {}).get("items", [])
                    
                    normalized = []
                    domain = self.config.bitable.domain
                    mapping = self.config.bitable.field_mapping
                    for item in items:
                        raw_fields = item.get("fields", {})
                        if keyword and not self._is_value_match(raw_fields.get(list(raw_fields.keys())[0]), keyword): # 简单的防暴力，实际上下面会全量遍历
                            # 因为此时没有任何服务器过滤，我们要对所有项进行 _is_value_match 检查
                            match = False
                            for val in raw_fields.values():
                                if self._is_value_match(val, keyword):
                                    match = True
                                    break
                            if not match:
                                continue

                        rec_id = item.get("record_id")
                        url = f"{domain}/base/{app_token}?table={table_id}&record={rec_id}" if domain else ""
                        mapped = apply_field_mapping(raw_fields, mapping)
                        normalized.append({
                            "record_id": rec_id,
                            "fields": mapped,
                            "fields_text": {str(k): str(v) for k, v in mapped.items()},
                            "record_url": url,
                        })
                    
                    return json.dumps({
                        "records": normalized,
                        "total": len(normalized),
                        "warning": "部分搜索字段不存在，已回退到全量匹配模式。"
                    }, ensure_ascii=False)
                except Exception as ex:
                    logger.error(f"Fallback search failed: {ex}")
                    return json.dumps({"error": str(ex), "records": []}, ensure_ascii=False)
            
            logger.error(f"Bitable search API error: {e}")
            return json.dumps({"error": str(e), "records": []}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Bitable search failed: {e}")
            return json.dumps({
                "error": str(e),
                "records": []
            }, ensure_ascii=False)


class BitableListTablesTool(Tool):
    """
    列出飞书多维表格 (Bitable) App 下的所有数据表。
    返回每张数据表的 table_id 与名称。
    """

    def __init__(self, config: FeishuDataConfig, client: FeishuDataClient):
        self.config = config
        self.client = client

    @property
    def name(self) -> str:
        return "bitable_list_tables"

    @property
    def description(self) -> str:
        return (
            "List all tables in a Feishu Bitable app. "
            "Returns table IDs and names."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app_token": {
                    "type": "string",
                    "description": "Bitable App Token. Defaults to config."
                }
            }
        }

    async def execute(self, **kwargs: Any) -> str:
        app_token = kwargs.get("app_token") or self.config.bitable.default_app_token
        if not app_token:
            return json.dumps({
                "error": "Missing app_token. Provide it as a parameter or configure a default.",
                "tables": []
            }, ensure_ascii=False)

        path = FeishuEndpoints.bitable_tables(app_token)
        try:
            res = await self.client.request("GET", path)
            items = res.get("data", {}).get("items", [])
            tables = [
                {"table_id": t.get("table_id"), "name": t.get("name", "")}
                for t in items
            ]
            return json.dumps({"tables": tables}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e), "tables": []}, ensure_ascii=False)


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

        view_id = kwargs.get("view_id") or self.config.bitable.default_view_id

        payload: dict[str, Any] = {}
        if view_id:
            payload["view_id"] = view_id

        # 日期区间过滤
        date_from = kwargs.get("date_from")
        date_to = kwargs.get("date_to")
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
        params = {"page_size": limit}

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
                        mapped = apply_field_mapping(fields, mapping)
                        matched.append({
                            "record_id": rec_id,
                            "fields": mapped,
                            "fields_text": {str(k): str(v) for k, v in mapped.items()},
                            "record_url": url,
                        })
                        break

            return json.dumps({
                "records": matched,
                "total": len(matched)
            }, ensure_ascii=False)
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
                            mapped = apply_field_mapping(fields, mapping)
                            matched.append({
                                "record_id": rec_id,
                                "fields": mapped,
                                "fields_text": {str(k): str(v) for k, v in mapped.items()},
                                "record_url": url,
                            })
                    
                    return json.dumps({
                        "records": matched,
                        "total": len(matched),
                        "warning": "部分人员搜索字段在表中未找到，已回退到全量扫描模式。"
                    }, ensure_ascii=False)
                except Exception as ex:
                    logger.error(f"search_person fallback failed: {ex}")
                    return json.dumps({"error": str(ex), "records": []}, ensure_ascii=False)
            
            logger.error(f"Bitable search_person API error: {e}")
            return json.dumps({"error": str(e), "records": []}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Bitable search_person failed: {e}")
            return json.dumps({"error": str(e), "records": []}, ensure_ascii=False)


# endregion
