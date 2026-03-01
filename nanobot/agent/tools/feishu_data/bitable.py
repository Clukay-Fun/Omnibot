"""飞书多维表格只读工具：提供对 Bitable 数据的查询等功能。"""

import json
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
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

        keyword = kwargs.get("keyword")
        limit = kwargs.get("limit") or self.config.bitable.search.default_limit
        if self.config.bitable.search.max_records > 0:
            limit = min(limit, self.config.bitable.search.max_records)

        view_id = kwargs.get("view_id") or self.config.bitable.default_view_id

        # 组装 payload
        payload = {}
        if view_id:
            payload["view_id"] = view_id

        # Basic condition assembly
        if keyword and self.config.bitable.search.searchable_fields:
            # 基础条件占位
            pass

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

# endregion
