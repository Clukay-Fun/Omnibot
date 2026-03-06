"""描述:
主要功能:
    - 基于模板规则执行文档字段提取与质量评估。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml


#region 抽取异常体系

class ExtractionError(RuntimeError):
    """
    用处: 文档抽取失败的兜底异常类型。

    功能:
        - 为该模块抛出的常规验证或操作失败提供捕获支点。
    """


class ExtractionQualityError(ExtractionError):
    """
    用处: 指示解析内容质量低于容忍阈值的特定错误。

    功能:
        - 记录明确缺少的必填字段，拦截未达标的数据流并辅助前端展示问题详情。
    """

    def __init__(self, message: str, *, missing_required_fields: list[str] | None = None):
        """
        用处: 构造低质量抽取报错。参数 message: 错误信息摘要，missing_required_fields: 缺失字段组。

        功能:
            - 绑定具体异常上下文文案及字段名。
        """
        super().__init__(message)
        self.missing_required_fields = list(missing_required_fields or [])

#endregion

#region 结构与实体定义

@dataclass(slots=True)
class ExtractFieldRule:
    """
    用处: 单个字段的匹配规则描述体。

    功能:
        - 定义目标键、关联的正则表达式表及是否强制截取的标定。
    """
    name: str
    patterns: list[str]
    required: bool = False


@dataclass(slots=True)
class ExtractTemplate:
    """
    用处: 面向整个类别文档的合并抽取模板结构。

    功能:
        - 根据文件分类包裹一批对应的抽取法则列。
    """
    template_id: str
    document_type: str
    fields: list[ExtractFieldRule] = field(default_factory=list)


@dataclass(slots=True)
class ExtractionResult:
    """
    用处: 文档参数提取最终成效容器。

    功能:
        - 承载命中返回的映射结果以及对应的宏观确信度表现。
    """
    template_id: str
    document_type: str
    fields: dict[str, str]
    missing_required_fields: list[str]
    confidence: float

#endregion

#region 引擎与规则评估函数

def load_extract_templates(workspace_root: Path | None = None) -> dict[str, ExtractTemplate]:
    """
    用处: 收集加载所有可用的文档字段抽取样式文件。参数 workspace_root: 本地用户的特化空间路径指针。

    功能:
        - 初始化默认随包模块及扫描检索用户私有工作区的 YAML 定义，发生重叠时完成数据遮蔽/覆盖接驳。
    """
    templates: dict[str, ExtractTemplate] = {}

    builtin_dir = files("nanobot") / "skills" / "extract_templates"
    for resource in builtin_dir.iterdir():
        if resource.name.endswith((".yaml", ".yml")):
            data = yaml.safe_load(resource.read_text(encoding="utf-8"))
            tpl = _parse_template(data, source=resource.name)
            templates[tpl.document_type] = tpl

    if workspace_root:
        for workspace_dir in _workspace_template_dirs(workspace_root):
            if not workspace_dir.exists():
                continue
            for path in sorted(list(workspace_dir.glob("*.yaml")) + list(workspace_dir.glob("*.yml"))):
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8"))
                    tpl = _parse_template(data, source=str(path))
                except (yaml.YAMLError, ExtractionError):
                    continue
                templates[tpl.document_type] = tpl

    return templates


def extract_fields(text: str, template: ExtractTemplate) -> ExtractionResult:
    """
    用处: 基于制定母版实施真实的信息提取行为。参数 text: 被解析成字符串的目标文档内容，template: 绑定的抽取策略。

    功能:
        - 按照规则依次尝试提取内容中的关键字词组，如强约束属性空缺则抛出低质警告，反则统计反馈置信概率量级。
    """
    values: dict[str, str] = {}
    missing_required: list[str] = []

    for field_rule in template.fields:
        matched = ""
        for pattern in field_rule.patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
            if match:
                matched = _normalize_match(match)
                if matched:
                    break
        if matched:
            values[field_rule.name] = matched
        elif field_rule.required:
            missing_required.append(field_rule.name)

    required_total = sum(1 for f in template.fields if f.required)
    confidence = _compute_confidence(values=values, missing_required=missing_required, required_total=required_total)
    if required_total and missing_required:
        missing_text = ", ".join(missing_required)
        raise ExtractionQualityError(
            "Low-quality extraction: missing required fields "
            f"[{missing_text}] for template '{template.template_id}'",
            missing_required_fields=missing_required,
        )

    return ExtractionResult(
        template_id=template.template_id,
        document_type=template.document_type,
        fields=values,
        missing_required_fields=missing_required,
        confidence=confidence,
    )


def _parse_template(data: dict[str, Any], source: str) -> ExtractTemplate:
    """
    用处: 将粗糙字典转换映射向数据定义类对象。参数 data: 解析好的 YAML 树，source: 这个设置的起源指向。

    功能:
        - 防范缺失段并约束生成有效的属性检索模板链。
    """
    if not isinstance(data, dict):
        raise ExtractionError(f"Invalid extract template in {source}: YAML object required")

    template_id = str(data.get("id") or "").strip()
    document_type = str(data.get("document_type") or "").strip()
    raw_fields = data.get("fields")
    if not template_id or not document_type:
        raise ExtractionError(f"Invalid extract template in {source}: id and document_type are required")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ExtractionError(f"Invalid extract template in {source}: fields must be a non-empty list")

    fields: list[ExtractFieldRule] = []
    for idx, item in enumerate(raw_fields):
        if not isinstance(item, dict):
            raise ExtractionError(f"Invalid field at index {idx} in {source}: object required")
        name = str(item.get("name") or "").strip()
        patterns = item.get("patterns")
        if not name or not isinstance(patterns, list) or not patterns:
            raise ExtractionError(
                f"Invalid field definition '{name or idx}' in {source}: "
                "name and non-empty patterns are required"
            )
        fields.append(
            ExtractFieldRule(
                name=name,
                patterns=[str(pattern) for pattern in patterns],
                required=bool(item.get("required", False)),
            )
        )

    return ExtractTemplate(template_id=template_id, document_type=document_type, fields=fields)


def _normalize_match(match: re.Match[str]) -> str:
    """
    用处: 过滤清洗抓取到的碎片段。参数 match: 正则截获的分组。

    功能:
        - 修建空泛符号使提取字符串平整规整。
    """
    value = match.group(1) if match.lastindex else match.group(0)
    return " ".join(value.strip().split())


def _compute_confidence(values: dict[str, str], missing_required: list[str], required_total: int) -> float:
    """
    用处: 通过抽成覆盖率计算解析水准打分。参数 values: 已提取键值对等。

    功能:
        - 根据必填与缺失字段数量提供百分比可信度（上限1下限0）。
    """
    if required_total == 0:
        return 1.0 if values else 0.0
    hit = required_total - len(missing_required)
    return max(0.0, min(1.0, hit / required_total))


def _workspace_template_dirs(workspace_root: Path) -> list[Path]:
    """
    用处: 定位专属的覆盖包路径序列。参数 workspace_root: 沙箱主目录系。

    功能:
        - 返回可能蕴含有定义集所在的预留路径坐标集。
    """
    return [
        workspace_root / "skillspec" / "extract",
        workspace_root / "extract",
    ]

#endregion
