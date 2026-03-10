"""飞书云文档搜索工具：提供对飞书云文档的关键词检索功能。"""

import json
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.endpoints import FeishuEndpoints
from nanobot.config.schema import FeishuDataConfig

# region [工具定义]


class DocSearchTool(Tool):
    """
    搜索飞书云文档。
    支持按关键词检索，可选限制为特定文件夹或文档类型。
    """

    def __init__(self, config: FeishuDataConfig, client: FeishuDataClient):
        self.config = config
        self.client = client

    @property
    def name(self) -> str:
        return "doc_search"

    @property
    def description(self) -> str:
        return (
            "Search Feishu cloud documents by keyword. "
            "Returns document titles, types, URLs, and preview snippets."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keyword for document content or title."
                },
                "folder_token": {
                    "type": "string",
                    "description": "Restrict search to a specific folder. Defaults to config."
                },
                "docs_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File types to include (e.g. 'doc', 'sheet', 'bitable'). Empty means all."
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of documents to return."
                }
            },
            "required": ["query"]
        }

    async def execute(self, **kwargs: Any) -> str:
        query = kwargs.get("query")
        if not query:
            return json.dumps({
                "error": "Missing query parameter.",
                "documents": []
            }, ensure_ascii=False)

        doc_cfg = self.config.doc.search
        limit = kwargs.get("limit") or doc_cfg.default_limit
        folder_token = kwargs.get("folder_token") or doc_cfg.default_folder_token
        docs_types = kwargs.get("docs_types")

        params: dict[str, Any] = {
            "search_key": query,
            "count": limit,
        }
        if folder_token:
            params["folder_token"] = folder_token
        if docs_types:
            # 飞书 Drive API 仅接受逗号分隔的字符串
            params["docs_types"] = ",".join(docs_types) if isinstance(docs_types, list) else docs_types

        path = FeishuEndpoints.doc_search()
        try:
            res = await self.client.request("GET", path, params=params)
            files = res.get("data", {}).get("files", [])
            preview_len = doc_cfg.preview_length

            documents = []
            for f in files:
                title = f.get("title", "")
                doc_type = f.get("type", "")
                token = f.get("token", "")
                url = f.get("url", "")
                # 截断预览内容
                preview = (title[:preview_len] + "...") if len(title) > preview_len else title

                documents.append({
                    "title": title,
                    "type": doc_type,
                    "token": token,
                    "url": url,
                    "preview": preview,
                })

            return json.dumps({
                "documents": documents,
                "total": len(documents),
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e), "documents": []}, ensure_ascii=False)


# endregion
