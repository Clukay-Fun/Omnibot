"""描述:
主要功能:
    - 加载并合并多来源的 SkillSpec 定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from yaml import YAMLError

from nanobot.agent.skill_runtime.spec_schema import (
    SkillSpec,
    SkillSpecActionStepBlueprint,
    SkillSpecBlueprint,
    SkillSpecTableTarget,
)

#region 统计与登记模型定义

@dataclass(slots=True)
class SkillSpecRegistryReport:
    """
    用处: 承载注册表启动加载阶段的明细盘点报告数据。

    功能:
        - 分类记载各规格配置文件的处理现状（如已载入、遭覆盖、产生冲突、被禁用或者无法解析的源项）。
    """
    loaded: list[str] = field(default_factory=list)
    overridden: list[str] = field(default_factory=list)
    source_collisions: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _SkillSpecRecord:
    """
    用处: 内部存储节点类，维系提取的技能规格参数与它的客观渊源关联。

    功能:
        - 将 Schema 和其所处具体文件路径、加载空间类别绑定在一起。
    """
    spec_id: str
    spec: SkillSpec
    path: Path
    source: str

#endregion

#region 模块级操作：启动时文件侦听与整合

class SkillSpecRegistry:
    """
    用处: 管理与校验来自各通道（包含内嵌包资源、代管应用、动态工作空间）配置的统一容器。

    功能:
        - 按优先级策略加载指定磁盘环境的所有定义，提供统一对象视图以应对随后的功能检索。
    """

    def __init__(self, workspace_root: Path, builtin_root: Path | None = None):
        """
        用处: 初始化注册表参数及待扫描基准位。参数 workspace_root: 动态生成区位，builtin_root: 包体资源位。

        功能:
            - 构建多级读取指针及准备载入明细表的数据模型容器。
        """
        self.workspace_root = workspace_root
        self.managed_root = workspace_root / "managed"
        self.builtin_root = builtin_root or Path(__file__).resolve().parents[2] / "skills" / "skillspec"
        self.report = SkillSpecRegistryReport()
        self._specs: dict[str, _SkillSpecRecord] = {}

    @property
    def specs(self) -> dict[str, SkillSpec]:
        """
        用处: 对象形式暴露出当前活跃的所有生效技能项字典。

        功能:
            - 提供 ID 到具体模型的便捷抽取管道，隐蔽底层复杂的源路径绑定对象。
        """
        return {spec_id: record.spec for spec_id, record in self._specs.items()}

    def load(self) -> dict[str, SkillSpec]:
        """
        用处: 触发全部配置的刷新读取流。

        功能:
            - 按内嵌、托管、工作树层级依次执行装入。
            - 处理跨空间覆写的情况及单文件内的多余配置剔除排布，将结局编排注入报告模块。
        """
        self.report = SkillSpecRegistryReport()
        layers = [
            ("bundled", self._load_root(self.builtin_root, source="bundled")),
            ("managed", self._load_root(self.managed_root, source="managed")),
            ("workspace", self._load_root(self.workspace_root, source="workspace")),
        ]

        merged: dict[str, _SkillSpecRecord] = {}
        for source, records in layers:
            for spec_id, record in records.items():
                previous = merged.get(spec_id)
                if previous is not None:
                    self.report.overridden.append(spec_id)
                    self.report.source_collisions.append(
                        f"{spec_id}: {previous.source}:{previous.path} -> {source}:{record.path}"
                    )
                merged[spec_id] = record

        active: dict[str, _SkillSpecRecord] = {}
        for spec_id, record in merged.items():
            if not record.spec.meta.enabled:
                self.report.disabled.append(spec_id)
                continue
            active[spec_id] = record

        self._specs = active
        self.report.loaded = sorted(active)
        self.report.overridden = sorted(set(self.report.overridden))
        self.report.source_collisions.sort()
        self.report.disabled.sort()
        self.report.invalid.sort()
        return self.specs

    @property
    def blueprints(self) -> dict[str, SkillSpecBlueprint]:
        """
        用处: 暴露当前已启用 SkillSpec 的归一化蓝图清单。

        功能:
            - 将运行时 skillspec 资产整理成未来工具定义生成可直接消费的只读模型。
        """
        return {
            spec_id: self._build_blueprint(record.spec)
            for spec_id, record in self._specs.items()
        }

    def get_blueprint(self, spec_id: str) -> SkillSpecBlueprint | None:
        """
        用处: 读取单个技能规格的归一化蓝图。

        功能:
            - 在外部按 ID 检索未来工具定义输入时提供稳定入口。
        """
        record = self._specs.get(spec_id)
        if record is None:
            return None
        return self._build_blueprint(record.spec)

    def _load_root(self, root: Path, source: str) -> dict[str, _SkillSpecRecord]:
        """
        用处: 承担逐库根节点的特定扫描解析派发动作。参数 root: 待搜集顶层路径，source: 这个位面的来源称谓。

        功能:
            - 提取符合文件要求范式的设置并在数据重名排他后转化成预定义的参数实体。
        """
        records: dict[str, _SkillSpecRecord] = {}
        if not root.exists():
            return records

        candidates = sorted(list(root.glob("*.yaml")) + list(root.glob("*.yml")))
        for path in candidates:
            if path.name.startswith("_"):
                continue
            try:
                raw = self._safe_load_yaml(path)
                if not isinstance(raw, dict):
                    raise ValueError("skillspec must be a YAML object")
                spec = SkillSpec.model_validate(raw)
                spec_id = str(spec.meta.id).strip()
                if not spec_id:
                    raise ValueError("meta.id must not be empty")
                record = _SkillSpecRecord(spec_id=spec_id, spec=spec, path=path, source=source)
                previous = records.get(spec_id)
                if previous is not None:
                    self.report.source_collisions.append(
                        f"{spec_id}: {source}:{previous.path} -> {source}:{record.path}"
                    )
                records[spec_id] = record
            except Exception as exc:
                self.report.invalid.append(f"{source}:{path.stem} ({exc})")
        return records

    @classmethod
    def _build_blueprint(cls, spec: SkillSpec) -> SkillSpecBlueprint:
        action = spec.action if isinstance(spec.action, dict) else {}
        params_schema = dict(spec.params) if isinstance(spec.params, dict) else {}
        action_kind = str(action.get("kind") or "").strip().lower()
        table = cls._normalize_table_target(action.get("table"))
        steps = cls._normalize_steps(action.get("cross_query"))

        tables: list[SkillSpecTableTarget] = []
        if table is not None:
            tables.append(table)
        for step in steps:
            if step.table is not None and step.table not in tables:
                tables.append(step.table)

        tool_refs = cls._collect_tool_refs(action)
        primary_tool = cls._primary_tool_for_action(action=action, action_kind=action_kind, tool_refs=tool_refs)

        has_cross_query = bool(steps)
        has_write_bridge = isinstance(action.get("write_bridge"), dict)
        action_metadata = {
            "has_cross_query": True if has_cross_query else None,
            "cross_query_mode": cls._clean_str((action.get("cross_query") or {}).get("mode")) if has_cross_query and isinstance(action.get("cross_query"), dict) else None,
            "pagination_mode": cls._clean_str(action.get("pagination_mode")) or cls._clean_str(spec.pagination_mode),
            "has_write_bridge": True if has_write_bridge else None,
            "bridge_keys": cls._collect_bridge_keys(action),
            "select_fields": cls._normalize_string_list(action.get("select_fields")),
        }

        return SkillSpecBlueprint(
            id=spec.meta.id,
            title=spec.meta.title,
            description=spec.meta.description,
            params_schema=params_schema,
            action_kind=action_kind,
            data_source=cls._clean_str(action.get("data_source")),
            action_target=cls._clean_str(action.get("target")),
            primary_tool=primary_tool,
            table=table,
            tables=tables,
            steps=steps,
            tool_refs=tool_refs,
            action_metadata={key: value for key, value in action_metadata.items() if value not in (None, [], {})},
        )

    @staticmethod
    def _clean_str(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None

    @classmethod
    def _normalize_table_target(cls, value: Any) -> SkillSpecTableTarget | None:
        if not isinstance(value, dict):
            return None
        payload = {
            "alias": cls._clean_str(value.get("alias") or value.get("table_alias")),
            "app_token": cls._clean_str(value.get("app_token")),
            "table_id": cls._clean_str(value.get("table_id")),
            "view_id": cls._clean_str(value.get("view_id")),
        }
        if not any(payload.values()):
            return None
        return SkillSpecTableTarget.model_validate(payload)

    @classmethod
    def _normalize_steps(cls, cross_query: Any) -> list[SkillSpecActionStepBlueprint]:
        if not isinstance(cross_query, dict):
            return []
        steps_raw = cross_query.get("steps")
        if not isinstance(steps_raw, list):
            return []

        normalized: list[SkillSpecActionStepBlueprint] = []
        for item in steps_raw:
            if not isinstance(item, dict):
                continue
            depends_on_value = item.get("depends_on")
            depends_on_raw = depends_on_value if isinstance(depends_on_value, list) else []
            depends_on = [str(dep).strip() for dep in depends_on_raw if str(dep).strip()]
            normalized.append(
                SkillSpecActionStepBlueprint(
                    id=cls._clean_str(item.get("id")),
                    kind=cls._clean_str(item.get("kind")),
                    data_source=cls._clean_str(item.get("data_source")),
                    target=cls._clean_str(item.get("target")),
                    tool=cls._clean_str(item.get("tool")),
                    depends_on=depends_on,
                    table=cls._normalize_table_target(item.get("table")),
                )
            )
        return normalized

    @classmethod
    def _collect_tool_refs(cls, action: dict[str, Any]) -> list[str]:
        refs: list[str] = []

        def _visit(value: Any) -> None:
            if isinstance(value, dict):
                tool_name = cls._clean_str(value.get("tool"))
                if tool_name and tool_name not in refs:
                    refs.append(tool_name)
                for nested in value.values():
                    _visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    _visit(nested)

        _visit(action)
        return refs

    @classmethod
    def _primary_tool_for_action(cls, *, action: dict[str, Any], action_kind: str, tool_refs: list[str]) -> str | None:
        explicit = cls._clean_str(action.get("tool"))
        if explicit:
            return explicit
        inferred = {
            "query": "bitable_search",
            "create": "bitable_create",
            "update": "bitable_update",
            "delete": "bitable_delete",
            "upsert": "bitable_upsert",
        }.get(action_kind)
        if inferred:
            return inferred
        return tool_refs[0] if tool_refs else None

    @classmethod
    def _collect_bridge_keys(cls, action: dict[str, Any]) -> list[str]:
        bridge_keys: list[str] = []
        for key, value in action.items():
            if not key.endswith("_bridge"):
                continue
            if not isinstance(value, dict):
                continue
            bridge_keys.append(key)
        return sorted(bridge_keys)

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @staticmethod
    def _safe_load_yaml(path: Path) -> object:
        """
        用处: 加强健壮性的针对性 YAML 回退载入处理，化解非规范转义语。参数 path: 实物路径。

        功能:
            - 针对配置文件作者习惯编写的双引号内字面正则表达式逃脱斜杠引发的崩溃做兜底再补偿解析机制。
        """
        text = path.read_text(encoding="utf-8")
        try:
            return yaml.safe_load(text)
        except YAMLError as exc:
            if "unknown escape character" not in str(exc):
                raise
            escaped_text = text.replace("\\", "\\\\")
            return yaml.safe_load(escaped_text)

#endregion
