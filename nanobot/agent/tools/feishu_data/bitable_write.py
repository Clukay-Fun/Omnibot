"""飞书多维表格写入工具：提供对 Bitable 数据的创建、更新和删除功能（两阶段安全机制）。"""

import json
import re
from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.confirm_store import ConfirmTokenStore
from nanobot.agent.tools.feishu_data.endpoints import FeishuEndpoints
from nanobot.config.schema import FeishuDataConfig

# region [写入工具]

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_DATE_ONLY_FORMATS = ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d")
_DATE_TIME_FORMATS = ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M", "%Y.%m.%d %H:%M")
_DATE_FIELD_RE = re.compile(r"(日|日期|date|deadline)$", re.IGNORECASE)


def _looks_like_date_field(field_name: str) -> bool:
    return bool(_DATE_FIELD_RE.search(field_name.strip()))


def _to_utc_millis(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp() * 1000)


def _normalize_date_field_value(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return value

        for fmt in _DATE_ONLY_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                local_midnight = datetime.combine(parsed.date(), time.min, tzinfo=_SHANGHAI_TZ)
                return _to_utc_millis(local_midnight)
            except ValueError:
                continue

        for fmt in _DATE_TIME_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt).replace(tzinfo=_SHANGHAI_TZ)
                return _to_utc_millis(parsed)
            except ValueError:
                continue
        return value

    if isinstance(value, (int, float)):
        ts_ms = int(value)
        if abs(ts_ms) < 10**11:
            ts_ms *= 1000
        dt_local = datetime.fromtimestamp(ts_ms / 1000, tz=_SHANGHAI_TZ)
        local_midnight = datetime.combine(dt_local.date(), time.min, tzinfo=_SHANGHAI_TZ)
        return _to_utc_millis(local_midnight)

    return value


def _normalize_fields_for_write(fields: Any) -> Any:
    if not isinstance(fields, dict):
        return fields
    normalized: dict[str, Any] = {}
    for key, value in fields.items():
        if _looks_like_date_field(str(key)):
            normalized[key] = _normalize_date_field_value(value)
        else:
            normalized[key] = value
    return normalized


class BitableCreateTool(Tool):
    """
    在飞书多维表格中创建新记录。
    默认以 dry_run 模式运行，返回操作预览和 confirm_token；
    确认后以 confirm_token 回传才实际执行写入。
    """

    def __init__(self, config: FeishuDataConfig, client: FeishuDataClient, store: ConfirmTokenStore):
        self.config = config
        self.client = client
        self.store = store

    @property
    def name(self) -> str:
        return "bitable_create"

    @property
    def description(self) -> str:
        return (
            "Create a new record in Feishu Bitable. "
            "First call returns a preview and confirm_token (dry_run). "
            "Pass confirm_token back to execute the actual write."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "object",
                    "description": "Record fields as key-value pairs (e.g., {'Name': 'Alice', 'Age': 30})."
                },
                "app_token": {
                    "type": "string",
                    "description": "Bitable App Token. Defaults to config."
                },
                "table_id": {
                    "type": "string",
                    "description": "Table ID. Defaults to config."
                },
                "confirm_token": {
                    "type": "string",
                    "description": "Token from a previous dry_run call to confirm the write."
                }
            },
            "required": ["fields"]
        }

    async def execute(self, **kwargs: Any) -> str:
        app_token = kwargs.get("app_token") or self.config.bitable.default_app_token
        table_id = kwargs.get("table_id") or self.config.bitable.default_table_id
        fields = kwargs.get("fields")
        confirm_token = kwargs.get("confirm_token")

        if not app_token or not table_id:
            return json.dumps({"error": "Missing app_token or table_id."}, ensure_ascii=False)
        if not fields:
            return json.dumps({"error": "Missing fields."}, ensure_ascii=False)

        fields = _normalize_fields_for_write(fields)

        # 构建操作负载（用于 token 绑定验证）
        op_payload = {"action": "create", "app_token": app_token, "table_id": table_id, "fields": fields}

        # 阶段 1：dry_run 预览
        if not confirm_token:
            token = self.store.create(op_payload)
            return json.dumps({
                "dry_run": True,
                "preview": {"action": "create", "fields": fields, "table_id": table_id},
                "confirm_token": token,
                "message": "请确认以上操作。将 confirm_token 传回本工具以执行写入。"
            }, ensure_ascii=False)

        # 阶段 2：确认执行
        if not self.store.consume(confirm_token, op_payload):
            return json.dumps({
                "error": "confirm_token 无效、已过期或操作负载不匹配。请重新发起 dry_run。"
            }, ensure_ascii=False)

        path = FeishuEndpoints.bitable_records(app_token, table_id)
        try:
            res = await self.client.request("POST", path, json_body={"fields": fields})
            record = res.get("data", {}).get("record", {})
            return json.dumps({
                "success": True,
                "record_id": record.get("record_id"),
                "fields": record.get("fields", {}),
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class BitableUpdateTool(Tool):
    """
    更新飞书多维表格中的已有记录。
    同样采用两阶段安全机制（dry_run + confirm_token）。
    """

    def __init__(self, config: FeishuDataConfig, client: FeishuDataClient, store: ConfirmTokenStore):
        self.config = config
        self.client = client
        self.store = store

    @property
    def name(self) -> str:
        return "bitable_update"

    @property
    def description(self) -> str:
        return (
            "Update an existing record in Feishu Bitable. "
            "First call returns a preview and confirm_token. "
            "Pass confirm_token back to execute the actual update."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "record_id": {
                    "type": "string",
                    "description": "The record ID to update."
                },
                "fields": {
                    "type": "object",
                    "description": "Fields to update as key-value pairs."
                },
                "app_token": {
                    "type": "string",
                    "description": "Bitable App Token. Defaults to config."
                },
                "table_id": {
                    "type": "string",
                    "description": "Table ID. Defaults to config."
                },
                "confirm_token": {
                    "type": "string",
                    "description": "Token from a previous dry_run call to confirm the update."
                }
            },
            "required": ["record_id", "fields"]
        }

    async def execute(self, **kwargs: Any) -> str:
        app_token = kwargs.get("app_token") or self.config.bitable.default_app_token
        table_id = kwargs.get("table_id") or self.config.bitable.default_table_id
        record_id = kwargs.get("record_id")
        fields = kwargs.get("fields")
        confirm_token = kwargs.get("confirm_token")

        if not app_token or not table_id:
            return json.dumps({"error": "Missing app_token or table_id."}, ensure_ascii=False)
        if not record_id:
            return json.dumps({"error": "Missing record_id."}, ensure_ascii=False)
        if not fields:
            return json.dumps({"error": "Missing fields."}, ensure_ascii=False)

        fields = _normalize_fields_for_write(fields)

        op_payload = {
            "action": "update", "app_token": app_token, "table_id": table_id,
            "record_id": record_id, "fields": fields,
        }

        if not confirm_token:
            token = self.store.create(op_payload)
            return json.dumps({
                "dry_run": True,
                "preview": {"action": "update", "record_id": record_id, "fields": fields},
                "confirm_token": token,
                "message": "请确认以上操作。将 confirm_token 传回本工具以执行更新。"
            }, ensure_ascii=False)

        if not self.store.consume(confirm_token, op_payload):
            return json.dumps({
                "error": "confirm_token 无效、已过期或操作负载不匹配。请重新发起 dry_run。"
            }, ensure_ascii=False)

        path = FeishuEndpoints.bitable_record(app_token, table_id, record_id)
        try:
            res = await self.client.request("PUT", path, json_body={"fields": fields})
            record = res.get("data", {}).get("record", {})
            return json.dumps({
                "success": True,
                "record_id": record.get("record_id", record_id),
                "fields": record.get("fields", {}),
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class BitableDeleteTool(Tool):
    """
    删除飞书多维表格中的记录。
    同样采用两阶段安全机制（dry_run + confirm_token）。
    """

    def __init__(self, config: FeishuDataConfig, client: FeishuDataClient, store: ConfirmTokenStore):
        self.config = config
        self.client = client
        self.store = store

    @property
    def name(self) -> str:
        return "bitable_delete"

    @property
    def description(self) -> str:
        return (
            "Delete a record from Feishu Bitable. "
            "First call returns a preview and confirm_token. "
            "Pass confirm_token back to execute the actual deletion."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "record_id": {
                    "type": "string",
                    "description": "The record ID to delete."
                },
                "app_token": {
                    "type": "string",
                    "description": "Bitable App Token. Defaults to config."
                },
                "table_id": {
                    "type": "string",
                    "description": "Table ID. Defaults to config."
                },
                "confirm_token": {
                    "type": "string",
                    "description": "Token from a previous dry_run call to confirm the deletion."
                }
            },
            "required": ["record_id"]
        }

    async def execute(self, **kwargs: Any) -> str:
        app_token = kwargs.get("app_token") or self.config.bitable.default_app_token
        table_id = kwargs.get("table_id") or self.config.bitable.default_table_id
        record_id = kwargs.get("record_id")
        confirm_token = kwargs.get("confirm_token")

        if not app_token or not table_id:
            return json.dumps({"error": "Missing app_token or table_id."}, ensure_ascii=False)
        if not record_id:
            return json.dumps({"error": "Missing record_id."}, ensure_ascii=False)

        op_payload = {"action": "delete", "app_token": app_token, "table_id": table_id, "record_id": record_id}

        if not confirm_token:
            token = self.store.create(op_payload)
            return json.dumps({
                "dry_run": True,
                "preview": {"action": "delete", "record_id": record_id, "table_id": table_id},
                "confirm_token": token,
                "message": "请确认以上删除操作。将 confirm_token 传回本工具以执行删除。"
            }, ensure_ascii=False)

        if not self.store.consume(confirm_token, op_payload):
            return json.dumps({
                "error": "confirm_token 无效、已过期或操作负载不匹配。请重新发起 dry_run。"
            }, ensure_ascii=False)

        path = FeishuEndpoints.bitable_record(app_token, table_id, record_id)
        try:
            await self.client.request("DELETE", path)
            return json.dumps({
                "success": True,
                "deleted_record_id": record_id,
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


# endregion
