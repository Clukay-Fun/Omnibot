"""Runtime text/template defaults without workspace overrides."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEFAULT_ROUTING: dict[str, dict[str, Any]] = {
    "smalltalk_triggers": {
        "direct_queries": [
            "你是谁",
            "您是谁",
            "你是干什么的",
            "您是干什么的",
            "你是做什么的",
            "您是做什么的",
            "你能干嘛",
            "您能干嘛",
            "你能做什么",
            "您能做什么",
            "你会什么",
            "您会什么",
            "你可以做什么",
            "您可以做什么",
            "怎么用你",
            "怎么使用你",
        ],
        "smalltalk_hints": [
            "你好",
            "您好",
            "hi",
            "hello",
            "哈喽",
            "在吗",
            "你是谁",
            "您是谁",
            "能干嘛",
            "能做什么",
            "做什么",
            "怎么用",
        ],
        "ability_subject_tokens": ["你", "您"],
        "ability_aux_tokens": ["能", "会", "可以"],
        "ability_action_tokens": ["干嘛", "做什么", "做啥", "怎么用", "帮什么"],
    },
    "preference_triggers": {
        "direct_queries": [
            "叫我什么",
            "我叫什么",
            "你叫我什么",
            "怎么叫我",
            "怎么称呼我",
            "称呼我什么",
            "我的称呼是什么",
            "你现在怎么称呼我",
        ],
        "contains_rules": [
            {"all": ["叫我", "什么"]},
            {"all": ["称呼我", "什么"]},
            {"all": ["怎么称呼我"]},
        ],
    },
    "pagination_triggers": {
        "continuation_commands": ["继续", "展开"],
    },
    "domain_hints": {
        "reminder_keywords": [
            "提醒",
            "提醒我",
            "截止",
            "到期",
            "闹钟",
            "日历",
            "calendar",
            "remind",
            "reminder",
            "due",
            "deadline",
        ],
        "cancel_intent_tokens": ["取消", "cancel", "删除", "close"],
        "business_keywords": [
            "案件",
            "合同",
            "投标",
            "任务",
            "提醒",
            "截止",
            "文档",
            "文件",
            "录入",
            "查询",
            "搜索",
            "开庭",
            "到期",
        ],
        "case_query_keywords": ["案子", "案件", "案号", "项目id", "开庭", "主办律师", "委托人"],
        "case_query_intent_tokens": [
            "查",
            "查下",
            "查一下",
            "查询",
            "搜索",
            "查找",
            "检索",
            "看看",
            "找",
            "找下",
            "找一下",
        ],
        "case_query_exclude_tokens": ["代办", "待办", "清单", "勾选", "卡片", "记一下", "记录"],
        "case_query_prefixes": ["请", "帮我", "麻烦", "查找", "查询", "搜索", "查下", "查一下", "看看", "找一下", "找"],
        "case_query_suffixes": ["案子", "案件", "案"],
        "template_case_keywords": ["案件", "案号", "诉讼", "仲裁", "case"],
        "template_contract_keywords": ["合同", "协议", "签约", "盖章", "付款条款", "contract", "ht"],
        "template_cross_keywords": ["跨表", "总览", "汇总", "overview", "全局"],
    },
}

_DEFAULT_TEMPLATES: dict[str, dict[str, Any]] = {
    "onboarding_form": {
        "header": {
            "title": "👋 你好，我是 Omnibot",
            "subtitle": "花 1 分钟做个设置，我能更好地帮你。",
        },
        "intro_markdown": "我可以帮你查询和管理案件、合同、投标信息，\n快速录入数据，设置提醒，识别文件。",
        "sections": {
            "identity": "**👤 基本信息**",
            "preferences": "**⚙️ 使用偏好**",
        },
        "labels": {
            "name": "怎么称呼您",
            "tone": "回复风格",
            "confirm_write": "录入数据时",
            "query_scope": "查案件时默认范围",
            "display_name": "您想怎么称呼我",
        },
        "placeholders": {
            "select": "请选择",
            "name": "不填则使用你的飞书昵称",
            "display_name": "选填",
        },
        "tone_options": [
            {"text": "简洁 — 只给关键信息", "value": "concise"},
            {"text": "标准 — 适当解释（默认）", "value": "standard"},
            {"text": "详细 — 完整说明", "value": "detailed"},
        ],
        "confirm_write_options": [
            {"text": "先给我确认再写入（默认）", "value": "yes"},
            {"text": "直接写入，不用每次确认", "value": "no"},
        ],
        "query_scope_options": [
            {"text": "只查我参与的", "value": "self"},
            {"text": "查全部", "value": "all"},
        ],
        "role_options_default": [
            {"text": "律师", "value": "lawyer"},
            {"text": "助理", "value": "assistant"},
            {"text": "实习生", "value": "intern"},
            {"text": "其他", "value": "other"},
        ],
        "team_options_default": [
            {"text": "诉讼一部", "value": "litigation_1"},
            {"text": "诉讼二部", "value": "litigation_2"},
            {"text": "非诉", "value": "non_litigation"},
            {"text": "其他", "value": "other"},
        ],
        "footer_hint": "以上设置随时可改。",
        "buttons": {
            "submit": "✅ 完成设置",
            "skip": "跳过，用默认值",
        },
        "completed_card_title": "设置完成 ✅",
    },
    "card_confirm": {
        "text": "写入预览：{preview}\n确认 {token} / 取消 {token}",
    },
    "card_case": {
        "header": "【案件卡片 | case.T1】",
        "lines": [
            "- 案件编号: {case_no}",
            "- 案件名称: {title}",
            "- 客户: {client}",
            "- 负责人: {owner}",
            "- 状态: {status}",
            "- 命中记录: {total}",
            "- 详情链接: {url}",
        ],
    },
    "card_contract": {
        "header": "【合同卡片 | contract.HT-T1】",
        "lines": [
            "- 合同编号: {contract_no}",
            "- 合同名称: {name}",
            "- 对方主体: {counterparty}",
            "- 负责人: {owner}",
            "- 合同金额: {amount}",
            "- 状态: {status}",
            "- 签订日期: {sign_date}",
            "- 命中记录: {total}",
            "- 详情链接: {url}",
        ],
    },
    "card_overview": {
        "header": "【跨表总览 | cross.OVERVIEW】",
        "source_title": "- 数据来源统计:",
        "preview_title": "- 结果预览:",
        "empty_source": "- 数据源: —",
        "empty_preview": "- —",
        "overflow_hint": "- ... 其余 {omitted} 条请回复“{continue_command}”",
    },
    "card_summary": {
        "header": "【摘要 | generic.SUMMARY】",
        "source_line": "- 数据来源: {sources}",
        "total_line": "- 命中总数: {total}",
        "next_step": "- 下一步建议: 请补充更具体的关键词，或回复“查看详情 <record_id>”。",
    },
}


@dataclass(slots=True)
class RuntimeTextCatalog:
    prompts: dict[str, dict[str, Any]]
    routing: dict[str, dict[str, Any]]
    templates: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, workspace: Path | None) -> "RuntimeTextCatalog":
        del workspace
        return cls(prompts={}, routing=deepcopy(_DEFAULT_ROUTING), templates=deepcopy(_DEFAULT_TEMPLATES))

    def prompt_text(self, group: str, key: str, default: str = "") -> str:
        value = self.prompts.get(group, {}).get(key)
        return str(value) if isinstance(value, str) else default

    def prompt_lines(self, group: str, key: str, default: list[str] | None = None) -> list[str]:
        value = self.prompts.get(group, {}).get(key)
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str):
            return [line for line in value.splitlines() if line.strip()]
        return list(default or [])

    def routing_list(self, group: str, key: str, default: list[str] | None = None) -> list[str]:
        value = self.routing.get(group, {}).get(key)
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return list(default or [])

    def template(self, name: str) -> dict[str, Any]:
        raw = self.templates.get(name)
        return deepcopy(raw) if isinstance(raw, dict) else {}
