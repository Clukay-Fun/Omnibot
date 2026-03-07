"""描述:
主要功能:
    - 加载并合并多来源的 SkillSpec 定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from yaml import YAMLError

from nanobot.agent.skill_runtime.spec_schema import SkillSpec

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
