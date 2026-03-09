"""飞书多维表格写入工具：提供对 Bitable 数据的创建、更新和删除功能（两阶段安全机制）。"""

import json
import re
from datetime import UTC, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.feishu_data.bitable import BitableListFieldsTool
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.confirm_store import ConfirmTokenStore
from nanobot.agent.tools.feishu_data.directory_config import load_directory_config
from nanobot.agent.tools.feishu_data.endpoints import FeishuEndpoints
from nanobot.agent.tools.feishu_data.person_resolver import BitablePersonResolver
from nanobot.config.schema import FeishuDataConfig

if TYPE_CHECKING:
    from nanobot.agent.turn_runtime import TurnRuntime

# region [写入工具]

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_DATE_ONLY_FORMATS = ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d")
_DATE_TIME_FORMATS = ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M", "%Y.%m.%d %H:%M")
_DATE_FIELD_RE = re.compile(r"(日|日期|date|deadline)$", re.IGNORECASE)
_PERSON_FIELD_TYPES = {11}
_SELF_PERSON_ALIASES = {"我", "本人", "我本人", "自己"}


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


class _BitableWriteToolBase(Tool):
    def __init__(
        self,
        config: FeishuDataConfig,
        client: FeishuDataClient,
        store: ConfirmTokenStore,
        *,
        workspace: Path | None = None,
    ):
        self.config = config
        self.client = client
        self.store = store
        self._workspace = workspace
        self._field_tool = BitableListFieldsTool(config, client)
        self._runtime_channel = ""
        self._runtime_chat_id = ""
        self._runtime_sender_id = ""
        self._runtime_metadata: dict[str, Any] = {}
        self._directory_config_cache: dict[str, Any] | None = None
        self._person_resolver: BitablePersonResolver | None = None

    def set_runtime_context(
        self,
        channel: str,
        chat_id: str,
        sender_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._runtime_channel = channel or ""
        self._runtime_chat_id = chat_id or ""
        self._runtime_sender_id = sender_id or ""
        self._runtime_metadata = dict(metadata or {})

    def set_turn_runtime(self, runtime: "TurnRuntime") -> None:
        self.set_runtime_context(runtime.channel, runtime.chat_id, runtime.sender_id, runtime.metadata)

    def _directory_config(self) -> dict[str, Any]:
        if self._directory_config_cache is not None:
            return dict(self._directory_config_cache)
        self._directory_config_cache = load_directory_config(self._workspace)
        return dict(self._directory_config_cache)

    def _ensure_person_resolver(self) -> BitablePersonResolver | None:
        directory = self._directory_config()
        if not directory.get("app_token") or not directory.get("table_id"):
            return None
        if self._person_resolver is None:
            self._person_resolver = BitablePersonResolver(self.config, client=self.client, directory=directory)
        return self._person_resolver

    async def _field_type_map(self, *, app_token: str, table_id: str) -> dict[str, int]:
        payload = json.loads(await self._field_tool.execute(app_token=app_token, table_id=table_id))
        if not isinstance(payload, dict) or payload.get("error"):
            return {}
        mapping: dict[str, int] = {}
        for item in payload.get("fields", []):
            if not isinstance(item, dict):
                continue
            field_name = str(item.get("field_name") or "").strip()
            field_type = item.get("type")
            if field_name and isinstance(field_type, int):
                mapping[field_name] = field_type
        return mapping

    def _runtime_open_id(self) -> str:
        return (
            str(self._runtime_metadata.get("sender_open_id") or "").strip()
            or str(self._runtime_sender_id or "").strip()
        )

    async def _resolve_person_open_ids(self, value: Any) -> list[str]:
        if value in (None, "", [], {}):
            return []
        if isinstance(value, list):
            resolved: list[str] = []
            for item in value:
                for current in await self._resolve_person_open_ids(item):
                    if current not in resolved:
                        resolved.append(current)
            return resolved
        if isinstance(value, dict):
            direct = str(value.get("open_id") or value.get("id") or "").strip()
            if direct.startswith("ou_"):
                return [direct]
            nested = str(value.get("name") or value.get("text") or "").strip()
            if nested:
                return await self._resolve_person_open_ids(nested)
            return []

        text = str(value or "").strip()
        if not text:
            return []
        if text in _SELF_PERSON_ALIASES:
            current_open_id = self._runtime_open_id()
            return [current_open_id] if current_open_id else []
        if text.startswith("ou_"):
            return [text]

        resolver = self._ensure_person_resolver()
        if resolver is None:
            return []
        resolved_open_id: str | None = await resolver.resolve(text)
        if isinstance(resolved_open_id, str) and resolved_open_id:
            return [resolved_open_id]
        return []

    async def _normalize_person_field_value(self, field_name: str, value: Any) -> Any:
        open_ids = await self._resolve_person_open_ids(value)
        if not open_ids:
            raise ValueError(
                f"人员字段“{field_name}”无法解析为 open_id。请配置 workspace/feishu/bitable_rules.yaml 的 directory，或直接传 open_id。"
            )
        return [{"id": open_id} for open_id in open_ids]

    async def _normalize_write_fields(self, *, app_token: str, table_id: str, fields: Any) -> Any:
        normalized = _normalize_fields_for_write(fields)
        if not isinstance(normalized, dict):
            return normalized
        if not self._runtime_open_id() and not self._directory_config():
            return normalized
        type_map = await self._field_type_map(app_token=app_token, table_id=table_id)
        if not type_map:
            return normalized

        result: dict[str, Any] = {}
        for key, value in normalized.items():
            field_name = str(key)
            if type_map.get(field_name) in _PERSON_FIELD_TYPES:
                result[field_name] = await self._normalize_person_field_value(field_name, value)
            else:
                result[field_name] = value
        return result


class BitableCreateTool(_BitableWriteToolBase):
    """
    在飞书多维表格中创建新记录。
    默认以 dry_run 模式运行，返回操作预览和 confirm_token；
    确认后以 confirm_token 回传才实际执行写入。
    """

    def __init__(
        self,
        config: FeishuDataConfig,
        client: FeishuDataClient,
        store: ConfirmTokenStore,
        workspace: Path | None = None,
    ):
        super().__init__(config, client, store, workspace=workspace)

    @property
    def name(self) -> str:
        return "bitable_create"

    @property
    def description(self) -> str:
        return (
            "Create a new record in Feishu Bitable. "
            "If table or fields are still unclear from natural language, call bitable_prepare_create first. "
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

        try:
            fields = await self._normalize_write_fields(app_token=app_token, table_id=table_id, fields=fields)
        except ValueError as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

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


class BitableUpdateTool(_BitableWriteToolBase):
    """
    更新飞书多维表格中的已有记录。
    同样采用两阶段安全机制（dry_run + confirm_token）。
    """

    def __init__(
        self,
        config: FeishuDataConfig,
        client: FeishuDataClient,
        store: ConfirmTokenStore,
        workspace: Path | None = None,
    ):
        super().__init__(config, client, store, workspace=workspace)

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

        try:
            fields = await self._normalize_write_fields(app_token=app_token, table_id=table_id, fields=fields)
        except ValueError as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

        op_payload = {
            "action": "update", "app_token": app_token, "table_id": table_id,
            "record_id": record_id, "fields": fields,
        }

        if not confirm_token:
            token = self.store.create(op_payload)
            return json.dumps({
                "dry_run": True,
                "preview": {"action": "update", "record_id": record_id, "table_id": table_id, "fields": fields},
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


class BitableDeleteTool(_BitableWriteToolBase):
    """
    删除飞书多维表格中的记录。
    同样采用两阶段安全机制（dry_run + confirm_token）。
    """

    def __init__(
        self,
        config: FeishuDataConfig,
        client: FeishuDataClient,
        store: ConfirmTokenStore,
        workspace: Path | None = None,
    ):
        super().__init__(config, client, store, workspace=workspace)

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
