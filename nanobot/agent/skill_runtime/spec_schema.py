"""描述:
主要功能:
    - 定义 SkillSpec 运行时校验所需的数据模型。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#region 基础模型定义

class _SpecBase(BaseModel):
    """
    用处: 基础 Schema 模型，允许传递额外的未定义字段。

    功能:
        - 作为其他具体技能配置片段的基类。
    """
    model_config = ConfigDict(extra="allow")

#endregion

#region 技能元数据模型

class SkillSpecMetaMatch(_SpecBase):
    """
    用处: 描述技能元数据中的匹配条件子模块。

    功能:
        - 存储匹配正则（regex）和匹配关键词（keywords）。
    """
    regex: str | None = None
    keywords: list[str] = Field(default_factory=list)


class SkillSpecMeta(_SpecBase):
    """
    用处: 描述技能的元数据信息（ID、版本、标题等）。

    功能:
        - 提供技能的全局基础信息定义。
    """
    id: str = Field(min_length=1)
    version: Literal["0.1"] = "0.1"
    title: str | None = None
    description: str | None = None
    enabled: bool = True
    match: SkillSpecMetaMatch | None = None

#endregion

#region 技能路由匹配模型

class SkillSpecMatch(_SpecBase):
    """
    用处: 定义技能被触发的详细匹配规则。

    功能:
        - 提供正则、关键词、渠道、发送者、意图等多种维度的匹配条件。
    """
    regex: str | None = None
    keywords: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    senders: list[str] = Field(default_factory=list)
    intents: list[str] = Field(default_factory=list)

#endregion

#region 技能输出策略模型

class SkillSpecOutputPolicy(_SpecBase):
    """
    用处: 定义技能执行结果的输出策略（如分页、截断）。

    功能:
        - 控制单次输出的最大字符数、最大条目数及分页缓存的存活时间。
    """
    max_chars: int | None = Field(default=None, ge=1)
    max_items: int | None = Field(default=None, ge=1)
    continuation_ttl_seconds: int | None = Field(default=None, ge=1)

#endregion

#region 技能蓝图模型

class SkillSpecTableTarget(_SpecBase):
    """
    用处: 表示技能动作里可直接抽取的表目标信息。

    功能:
        - 为后续工具定义生成阶段保留表别名与显式表 ID 元数据。
    """

    alias: str | None = None
    app_token: str | None = None
    table_id: str | None = None
    view_id: str | None = None


class SkillSpecActionStepBlueprint(_SpecBase):
    """
    用处: 描述跨查询等复合动作中的单步蓝图。

    功能:
        - 统一暴露步骤 ID、依赖、数据源、工具与表目标等只读信息。
    """

    id: str | None = None
    kind: str | None = None
    data_source: str | None = None
    target: str | None = None
    tool: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    table: SkillSpecTableTarget | None = None


class SkillSpecBlueprint(_SpecBase):
    """
    用处: 为每条 SkillSpec 提供可转化为未来工具定义输入的归一化只读蓝图。

    功能:
        - 汇总技能的标识、描述、参数 Schema 与动作层元数据。
        - 保留显式表目标、跨查询步骤和桥接工具等结构化资产信息。
    """

    id: str = Field(min_length=1)
    title: str | None = None
    description: str | None = None
    params_schema: dict[str, Any] = Field(default_factory=dict)
    action_kind: str = ""
    data_source: str | None = None
    action_target: str | None = None
    primary_tool: str | None = None
    table: SkillSpecTableTarget | None = None
    tables: list[SkillSpecTableTarget] = Field(default_factory=list)
    steps: list[SkillSpecActionStepBlueprint] = Field(default_factory=list)
    tool_refs: list[str] = Field(default_factory=list)
    action_metadata: dict[str, Any] = Field(default_factory=dict)

#endregion

#region 顶层配置模型

class SkillSpec(BaseModel):
    """
    用处: 表示完整的技能规范配置文档。

    功能:
        - 组装包含元数据、参数、动作、响应、异常处理等在内的完整配置对象。
        - 严格禁止传入未在模型中定义的额外字段。
    """

    model_config = ConfigDict(extra="forbid")

    meta: SkillSpecMeta
    params: dict[str, Any] = Field(default_factory=dict)
    action: dict[str, Any]
    response: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] = Field(default_factory=dict)
    match: SkillSpecMatch | None = None
    output_policy: SkillSpecOutputPolicy | None = None
    pagination_mode: Literal["none", "offset", "cursor"] | None = None

#endregion
