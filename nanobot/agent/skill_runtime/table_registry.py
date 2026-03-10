"""描述:
主要功能:
    - 管理可覆盖的飞书数据表别名与字段映射。
"""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from nanobot.agent.skill_runtime.table_profile_cache import TableProfileCache, schema_hash_for_fields
from nanobot.agent.skill_runtime.table_profile_synthesizer import TableProfileSynthesizer

#region 表注册表

class TableRegistry:
    """用处，参数

    功能:
        - 提供表别名解析和字段映射能力。
    """

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        builtin_path: Path | None = None,
        profile_synthesizer: TableProfileSynthesizer | None = None,
    ):
        """用处，参数

        功能:
            - 初始化内置与工作区映射文件路径。
        """
        self._workspace = workspace
        self._builtin_path = builtin_path or Path(__file__).resolve().parents[2] / "skills" / "table_registry.yaml"
        self._workspace_path = (workspace / "skills" / "table_registry.yaml") if workspace else None
        self._profile_cache = TableProfileCache(workspace=workspace)
        self._profile_synthesizer = profile_synthesizer
        self._tables: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._last_mtimes: tuple[int | None, int | None] = (None, None)

    def resolve_table(self, alias: str) -> dict[str, Any] | None:
        """用处，参数

        功能:
            - 按逻辑别名返回目标表配置。
        """
        self._ensure_loaded()
        key = alias.strip()
        if not key:
            return None
        raw = self._tables.get(key)
        if not isinstance(raw, dict):
            return None
        resolved = {
            "app_token": raw.get("app_token"),
            "table_id": raw.get("table_id"),
            "view_id": raw.get("view_id"),
        }
        return {k: v for k, v in resolved.items() if isinstance(v, str) and v.strip()}

    def map_field(self, alias: str, logical_name: str) -> str:
        """用处，参数

        功能:
            - 将逻辑字段名映射为真实字段名。
        """
        self._ensure_loaded()
        table = self._tables.get(alias.strip())
        if not isinstance(table, dict):
            return logical_name
        aliases_raw = table.get("field_aliases")
        if not isinstance(aliases_raw, dict):
            return logical_name
        lookup = logical_name.strip()
        if not lookup:
            return logical_name

        direct = aliases_raw.get(lookup)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        lowered = lookup.lower()
        for key, value in aliases_raw.items():
            if str(key).strip().lower() != lowered:
                continue
            if isinstance(value, str) and value.strip():
                return value.strip()
        return logical_name

    def map_fields(self, alias: str, fields: dict[str, Any]) -> dict[str, Any]:
        """用处，参数

        功能:
            - 批量映射字段字典中的键名。
        """
        mapped: dict[str, Any] = {}
        for key, value in fields.items():
            field_name = str(key)
            mapped[self.map_field(alias, field_name)] = value
        return mapped

    def map_filters(self, alias: str, filters: dict[str, Any]) -> dict[str, Any]:
        """用处，参数

        功能:
            - 递归映射过滤器中的字段名。
        """
        self._ensure_loaded()

        def _map_value(value: Any) -> Any:
            """用处，参数

            功能:
                - 递归处理字典与列表节点。
            """
            if isinstance(value, dict):
                out: dict[str, Any] = {}
                for k, v in value.items():
                    if k == "field_name" and isinstance(v, str):
                        out[k] = self.map_field(alias, v)
                    else:
                        out[k] = _map_value(v)
                return out
            if isinstance(value, list):
                return [_map_value(item) for item in value]
            return value

        mapped = _map_value(filters)
        return mapped if isinstance(mapped, dict) else dict(filters)

    def aliases(self) -> list[str]:
        """用处，参数

        功能:
            - 返回当前可用别名列表。
        """
        self._ensure_loaded()
        return sorted(self._tables.keys())

    def get_table_metadata(self, alias: str) -> dict[str, Any] | None:
        self._ensure_loaded()
        payload = self._tables.get(alias.strip())
        return dict(payload) if isinstance(payload, dict) else None

    def find_alias(self, *, app_token: str, table_id: str, table_name: str = "") -> str | None:
        self._ensure_loaded()
        display_name = table_name.strip()
        for alias, payload in self._tables.items():
            if not isinstance(payload, dict):
                continue
            if app_token and table_id:
                if str(payload.get("app_token") or "").strip() == app_token and str(payload.get("table_id") or "").strip() == table_id:
                    return alias
            if display_name:
                if str(payload.get("display_name") or "").strip() == display_name:
                    return alias
                aliases_raw = payload.get("aliases")
                aliases: list[str] = [
                    str(item).strip()
                    for item in aliases_raw
                    if isinstance(item, str) and str(item).strip()
                ] if isinstance(aliases_raw, list) else []
                if display_name in set(aliases):
                    return alias
        return None

    def get_table_profile(self, alias: str, *, table_name: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
        self._ensure_loaded()
        key = alias.strip()
        table = self._tables.get(key) if key else None
        table_payload = table if isinstance(table, dict) else {}
        app_token = str(table_payload.get("app_token") or "").strip()
        table_id = str(table_payload.get("table_id") or "").strip()
        schema_hash = schema_hash_for_fields(fields)
        if app_token and table_id:
            cached = self._profile_cache.get(app_token=app_token, table_id=table_id, schema_hash=schema_hash)
            if cached is not None:
                return cached

        profile = self._build_table_profile(alias=key or alias, table_name=table_name, fields=fields, metadata=table_payload)
        if app_token and table_id:
            return self._profile_cache.put(app_token=app_token, table_id=table_id, schema_hash=schema_hash, profile=profile)
        return profile

    async def get_or_synthesize_table_profile(self, alias: str, *, table_name: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
        heuristic = self.get_table_profile(alias, table_name=table_name, fields=fields)
        if heuristic.get("source") == "llm" or self._profile_synthesizer is None:
            return heuristic

        key = alias.strip()
        table = self._tables.get(key) if key else None
        table_payload = table if isinstance(table, dict) else {}
        app_token = str(table_payload.get("app_token") or "").strip()
        table_id = str(table_payload.get("table_id") or "").strip()
        if not app_token or not table_id:
            return heuristic

        synthesized = await self._profile_synthesizer.synthesize(
            alias=key or alias,
            table_name=table_name,
            fields=fields,
            seed_profile=heuristic,
        )
        if not isinstance(synthesized, dict):
            return heuristic

        merged = dict(heuristic)
        llm_aliases = [
            str(item).strip()
            for item in synthesized.get("aliases", [])
            if isinstance(item, str) and str(item).strip()
        ] if isinstance(synthesized.get("aliases"), list) else []
        merged["aliases"] = list(dict.fromkeys([*heuristic.get("aliases", []), *llm_aliases]))
        for key_name in ("purpose_guess", "common_query_patterns", "common_write_patterns", "confidence"):
            value = synthesized.get(key_name)
            if isinstance(value, list):
                cleaned = [str(item).strip() for item in value if str(item).strip()]
                if cleaned:
                    merged[key_name] = cleaned
            elif isinstance(value, str) and value.strip():
                merged[key_name] = value.strip()
        merged["source"] = "llm"
        return self._profile_cache.put(
            app_token=app_token,
            table_id=table_id,
            schema_hash=schema_hash_for_fields(fields),
            profile=merged,
        )

    def get_latest_profile(self, *, app_token: str, table_id: str) -> dict[str, Any] | None:
        return self._profile_cache.get_latest(app_token=app_token, table_id=table_id)

    @staticmethod
    def _match_any(text: str, tokens: tuple[str, ...]) -> bool:
        lowered = text.lower()
        return any(token in lowered for token in tokens)

    def _build_table_profile(
        self,
        *,
        alias: str,
        table_name: str,
        fields: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        display_name = str(metadata.get("display_name") or table_name or alias).strip() or alias
        alias_items = metadata.get("aliases") if isinstance(metadata.get("aliases"), list) else []
        configured_aliases = [
            str(item).strip()
            for item in alias_items
            if isinstance(item, str) and str(item).strip()
        ]
        aliases: list[str] = []
        for item in [display_name, alias, *configured_aliases, *self._default_aliases(display_name, alias)]:
            candidate = str(item or "").strip()
            if candidate and candidate not in aliases:
                aliases.append(candidate)

        field_roles: dict[str, str] = {}
        person_fields: list[str] = []
        time_fields: list[str] = []
        status_fields: list[str] = []
        for item in fields:
            field_name = str(item.get("field_name") or item.get("name") or "").strip()
            if not field_name:
                continue
            role = self._infer_field_role(display_name, field_name)
            if role:
                field_roles[field_name] = role
            field_type = item.get("type")
            if field_type == 11 or role in {"owner", "assignee", "lawyer", "person"}:
                if field_name not in person_fields:
                    person_fields.append(field_name)
            if role in {"time", "date", "deadline", "week", "created_at", "updated_at", "completed_at", "expiry_date"}:
                if field_name not in time_fields:
                    time_fields.append(field_name)
            if role == "status" and field_name not in status_fields:
                status_fields.append(field_name)

        identity_fields_guess = self._identity_fields(display_name, field_roles)
        identity_strategies = self._identity_strategies(display_name, field_roles)
        schema_hash = schema_hash_for_fields(fields)
        return {
            "alias": alias,
            "display_name": display_name,
            "aliases": aliases,
            "purpose_guess": self._purpose_guess(display_name),
            "field_roles": field_roles,
            "identity_fields_guess": identity_fields_guess,
            "identity_strategies": identity_strategies,
            "person_fields": person_fields,
            "time_fields": time_fields,
            "status_fields": status_fields,
            "common_query_patterns": self._common_query_patterns(display_name),
            "common_write_patterns": self._common_write_patterns(display_name),
            "confidence": "medium",
            "schema_hash": schema_hash,
            "source": "heuristic",
        }

    def _default_aliases(self, display_name: str, alias: str) -> list[str]:
        text = f"{display_name} {alias}".lower()
        if self._match_any(text, ("案件", "case", "project")):
            return ["案件项目总库", "案件库", "项目总库", "案件"]
        if self._match_any(text, ("合同", "contract", "agreement")):
            return ["合同管理", "合同台账", "合同库", "合同"]
        if self._match_any(text, ("周", "weekly", "plan", "工作计划")):
            return ["团队周工作计划表", "周工作计划", "周计划", "周报表"]
        return []

    def _purpose_guess(self, display_name: str) -> str:
        text = display_name.lower()
        if self._match_any(text, ("案件", "case", "project")):
            return "案件项目主档与阶段状态跟踪"
        if self._match_any(text, ("合同", "contract", "agreement")):
            return "合同登记、状态跟踪与到期管理"
        if self._match_any(text, ("周", "weekly", "工作计划", "周报")):
            return "团队每周工作计划与进展记录"
        return "基于当前表结构自动推断的业务表"

    def _common_query_patterns(self, display_name: str) -> list[str]:
        text = display_name.lower()
        if self._match_any(text, ("案件", "case", "project")):
            return ["查某个案件", "按案号查项目", "看案件状态"]
        if self._match_any(text, ("合同", "contract", "agreement")):
            return ["查合同状态", "按合同编号找合同", "看快到期合同"]
        if self._match_any(text, ("周", "weekly", "工作计划", "周报")):
            return ["查这周计划", "补本周工作", "看谁的周计划"]
        return ["按关键词查询这张表"]

    def _common_write_patterns(self, display_name: str) -> list[str]:
        text = display_name.lower()
        if self._match_any(text, ("案件", "case", "project")):
            return ["更新案件状态", "补充案件节点", "修改主办律师"]
        if self._match_any(text, ("合同", "contract", "agreement")):
            return ["登记新合同", "更新合同状态", "补充到期时间"]
        if self._match_any(text, ("周", "weekly", "工作计划", "周报")):
            return ["新增周计划", "更新本周进展", "按姓名和周次补写计划"]
        return ["创建或更新表记录"]

    def _identity_fields(self, display_name: str, field_roles: dict[str, str]) -> list[str]:
        role_to_fields: dict[str, list[str]] = {}
        for field_name, role in field_roles.items():
            role_to_fields.setdefault(role, []).append(field_name)
        text = display_name.lower()
        if self._match_any(text, ("案件", "case", "project")):
            return [*role_to_fields.get("case_no", []), *role_to_fields.get("case_id", [])]
        if self._match_any(text, ("合同", "contract", "agreement")):
            return [*role_to_fields.get("contract_no", []), *role_to_fields.get("title", [])]
        if self._match_any(text, ("周", "weekly", "工作计划", "周报")):
            return [*role_to_fields.get("owner", []), *role_to_fields.get("week", [])]
        return []

    def _identity_strategies(self, display_name: str, field_roles: dict[str, str]) -> list[list[str]]:
        role_to_fields: dict[str, list[str]] = {}
        for field_name, role in field_roles.items():
            role_to_fields.setdefault(role, []).append(field_name)
        text = display_name.lower()
        if self._match_any(text, ("案件", "case", "project")):
            strategies = [role_to_fields.get("case_no", []), role_to_fields.get("case_id", [])]
        elif self._match_any(text, ("合同", "contract", "agreement")):
            strategies = [role_to_fields.get("contract_no", []), role_to_fields.get("title", [])]
        elif self._match_any(text, ("周", "weekly", "工作计划", "周报")):
            owner_fields = role_to_fields.get("owner", [])
            week_fields = role_to_fields.get("week", [])
            strategies = [[*owner_fields, *week_fields]] if owner_fields or week_fields else []
        else:
            strategies = []
        normalized: list[list[str]] = []
        for strategy in strategies:
            cleaned = [str(item).strip() for item in strategy if str(item).strip()]
            if cleaned and cleaned not in normalized:
                normalized.append(cleaned)
        return normalized

    def _infer_field_role(self, display_name: str, field_name: str) -> str | None:
        text = field_name.strip().lower()
        if self._match_any(text, ("项目id", "项目编号", "case_id")):
            return "case_id"
        if self._match_any(text, ("案号", "case_no")):
            return "case_no"
        if self._match_any(text, ("合同编号", "contract_no")):
            return "contract_no"
        if self._match_any(text, ("合同名称", "协议名称", "标题", "名称", "title")):
            return "title"
        if self._match_any(text, ("主办律师", "负责人", "owner", "assignee", "经办")):
            return "owner"
        if self._match_any(text, ("委托人", "客户", "client")):
            return "client"
        if self._match_any(text, ("乙方", "vendor", "供应商")):
            return "vendor"
        if self._match_any(text, ("金额", "amount", "价税", "合同金额")):
            return "amount"
        if self._match_any(text, ("周次", "week", "本周")):
            return "week"
        if self._match_any(text, ("工作内容", "计划", "事项", "进展")):
            return "content"
        if self._match_any(text, ("完成时间", "completed_at")):
            return "completed_at"
        if self._match_any(text, ("到期", "截止", "时间", "日期", "deadline", "date", "renewal", "创建时间", "更新时间")):
            if self._match_any(text, ("创建时间", "created_at")):
                return "created_at"
            if self._match_any(text, ("更新时间", "updated_at")):
                return "updated_at"
            if self._match_any(text, ("到期", "expiry")):
                return "expiry_date"
            if self._match_any(text, ("截止", "deadline", "节点")):
                return "deadline"
            return "time"
        if self._match_any(text, ("状态", "status")):
            return "status"
        if self._match_any(display_name.lower(), ("周", "weekly", "工作计划", "周报")) and self._match_any(text, ("姓名", "成员", "人员")):
            return "owner"
        return None

    def _ensure_loaded(self) -> None:
        """用处，参数

        功能:
            - 按文件更新时间自动刷新映射缓存。
        """
        builtin_mtime = self._safe_mtime(self._builtin_path)
        workspace_mtime = self._safe_mtime(self._workspace_path)
        current = (builtin_mtime, workspace_mtime)
        if self._loaded and current == self._last_mtimes:
            return

        merged: dict[str, dict[str, Any]] = {}
        self._merge_layer(merged, self._load_tables(self._builtin_path, strip_example_ids=True))
        self._merge_layer(merged, self._load_tables(self._workspace_path, strip_example_ids=False))

        self._tables = merged
        self._last_mtimes = current
        self._loaded = True

    @staticmethod
    def _safe_mtime(path: Path | None) -> int | None:
        """用处，参数

        功能:
            - 安全读取文件修改时间戳。
        """
        if path is None or not path.exists():
            return None
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return None

    @staticmethod
    def _merge_layer(base: dict[str, dict[str, Any]], overlay: dict[str, dict[str, Any]]) -> None:
        """用处，参数

        功能:
            - 将覆盖层合并到基础映射中。
        """
        for alias, payload in overlay.items():
            existing = base.get(alias)
            if isinstance(existing, dict):
                merged = deepcopy(existing)
                merged.update(payload)
                base[alias] = merged
            else:
                base[alias] = deepcopy(payload)

    @staticmethod
    def _looks_like_example_id(*, token: str, value: str) -> bool:
        """用处，参数

        功能:
            - 识别内置示例占位符 ID 以避免误请求线上接口。
        """
        normalized = value.strip().lower()
        if token == "app_token":
            return bool(re.fullmatch(r"app_[a-z0-9_]+_[0-9]{3,}", normalized))
        if token == "table_id":
            return bool(re.fullmatch(r"tbl_[a-z0-9_]+_[0-9]{3,}", normalized))
        return False

    @staticmethod
    def _load_tables(path: Path | None, *, strip_example_ids: bool) -> dict[str, dict[str, Any]]:
        """用处，参数

        功能:
            - 从 YAML 文件读取并规范化表配置。
        """
        if path is None or not path.exists():
            return {}
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(loaded, dict):
            return {}
        tables = loaded.get("tables")
        if not isinstance(tables, dict):
            return {}

        normalized: dict[str, dict[str, Any]] = {}
        for alias, value in tables.items():
            key = str(alias).strip()
            if not key or not isinstance(value, dict):
                continue
            record: dict[str, Any] = {}
            for token in ("app_token", "table_id", "view_id"):
                item = value.get(token)
                if isinstance(item, str) and item.strip():
                    cleaned = item.strip()
                    if strip_example_ids and TableRegistry._looks_like_example_id(token=token, value=cleaned):
                        continue
                    record[token] = cleaned
            display_name = str(value.get("display_name") or "").strip()
            if display_name:
                record["display_name"] = display_name
            aliases_raw = value.get("aliases")
            if isinstance(aliases_raw, list):
                aliases = [str(item).strip() for item in aliases_raw if str(item).strip()]
                if aliases:
                    record["aliases"] = aliases
            aliases_raw = value.get("field_aliases")
            if isinstance(aliases_raw, dict):
                field_aliases: dict[str, str] = {}
                for k, v in aliases_raw.items():
                    source = str(k).strip()
                    target = str(v).strip()
                    if source and target:
                        field_aliases[source] = target
                if field_aliases:
                    record["field_aliases"] = field_aliases
            if record:
                normalized[key] = record
        return normalized


#endregion
