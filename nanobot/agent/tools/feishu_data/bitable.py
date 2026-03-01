"""飞书多维表格只读工具：提供对 Bitable 数据的查询等功能。"""

import json
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.date_utils import build_date_filter
from nanobot.agent.tools.feishu_data.endpoints import FeishuEndpoints
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

    @property
    def name(self) -> str:
        return "bitable_search"

    @property
    def description(self) -> str:
        return (
            "Search for records in Feishu Bitable. "
            "Use this tool to read and query tabular data."
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

        try:
            res = await self.client.request("POST", path, params=params, json_body=payload)
            items = res.get("data", {}).get("items", [])
            normalized = []
            domain = self.config.bitable.domain
            for item in items:
                rec_id = item.get("record_id")
                url = f"{domain}/base/{app_token}?table={table_id}&record={rec_id}" if domain else ""
                normalized.append({
                    "record_id": rec_id,
                    "fields": item.get("fields", {}),
                    "fields_text": {str(k): str(v) for k, v in item.get("fields", {}).items()},
                    "record_url": url,
                })

            return json.dumps({
                "records": normalized,
                "total": res.get("data", {}).get("total", len(normalized))
            }, ensure_ascii=False)

        except Exception as e:
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
            return json.dumps({
                "record": {
                    "record_id": record.get("record_id", record_id),
                    "fields": record.get("fields", {}),
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
        date_field = self.config.bitable.search.date_field
        date_filter = build_date_filter(date_field, date_from, date_to)
        if date_filter:
            payload["filter"] = date_filter

        path = FeishuEndpoints.bitable_records_search(app_token, table_id)
        params = {"page_size": limit}

        try:
            res = await self.client.request("POST", path, params=params, json_body=payload)
            items = res.get("data", {}).get("items", [])

            # 在客户端侧按 person_name 筛选含有匹配人员字段的记录
            matched = []
            domain = self.config.bitable.domain
            for item in items:
                fields = item.get("fields", {})
                for _field_name, field_val in fields.items():
                    # 飞书人员字段值为 list[dict]，每个 dict 含 name 字段
                    if isinstance(field_val, list):
                        for entry in field_val:
                            if isinstance(entry, dict) and person_name.lower() in str(entry.get("name", "")).lower():
                                rec_id = item.get("record_id")
                                url = f"{domain}/base/{app_token}?table={table_id}&record={rec_id}" if domain else ""
                                matched.append({
                                    "record_id": rec_id,
                                    "fields": fields,
                                    "fields_text": {str(k): str(v) for k, v in fields.items()},
                                    "record_url": url,
                                })
                                break
                    # 文本型人员字段（直接字符串值）
                    elif isinstance(field_val, str) and person_name.lower() in field_val.lower():
                        rec_id = item.get("record_id")
                        url = f"{domain}/base/{app_token}?table={table_id}&record={rec_id}" if domain else ""
                        matched.append({
                            "record_id": rec_id,
                            "fields": fields,
                            "fields_text": {str(k): str(v) for k, v in fields.items()},
                            "record_url": url,
                        })
                        break

            return json.dumps({
                "records": matched,
                "total": len(matched)
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e), "records": []}, ensure_ascii=False)


# endregion
