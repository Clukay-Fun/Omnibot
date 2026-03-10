"""Runtime text/template defaults with optional workspace overrides."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

_OVERRIDE_FILENAMES = ("runtime_texts.yaml", "runtime_texts.yml", "runtime_texts.json")


def _deep_merge_dict(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    for key, value in overlay.items():
        existing = base.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            _deep_merge_dict(existing, value)
        else:
            base[key] = deepcopy(value)
    return base


def _load_override_payload(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to read runtime text override {}: {}", path, exc)
        return {}

    try:
        if path.suffix.lower() == ".json":
            payload = json.loads(text)
        else:
            payload = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse runtime text override {}: {}", path, exc)
        return {}

    return payload if isinstance(payload, dict) else {}

_DEFAULT_PROMPTS: dict[str, dict[str, Any]] = {
    "help": {
        "commands_help_text": (
            "全局命令\n"
            "- /help 或 /commands：查看命令总览\n"
            "- /status：查看当前偏好与授权状态\n"
            "- /plan：切换到计划模式（只分析/规划，不执行 skill 或工具）\n"
            "- /build：切换到构建模式（允许执行 skill 和工具）\n"
            "- /setup：查看初始化引导\n"
            "- /connect 或 /oauth：连接飞书 OAuth\n"
            "- /session：查看会话子命令帮助\n"
            "- /new：开启新会话\n"
            "- /stop：停止当前任务\n"
            "\n上下文命令\n"
            "- 继续 / 展开：仅当当前有分页结果时，查看剩余内容\n"
            "- 确认 <token> / 取消 <token>：仅当当前有写入预览时，确认或取消写入"
        ),
        "session_help_text": (
            "会话命令\n"
            "- /session：查看帮助\n"
            "- /session new [标题]：在群话题中新建会话\n"
            "- /session list：列出当前聊天下会话\n"
            "- /session del <id|main>：删除指定会话"
        ),
    },
    "pagination": {
        "no_more_content": "没有可继续的内容了。",
        "continuation_hint": "回复“{continue_command}”查看剩余内容",
        "not_found_data": "未查询到数据。",
    },
    "onboarding": {
        "intro_first": "我先按 BOOTSTRAP 默认继续，也把确认方式发你，不影响继续提问。",
        "intro_reentry": "已重新打开 BOOTSTRAP 确认提示。",
        "guide_lines": [
            "### 👋 BOOTSTRAP 确认",
            "我会先按 `BOOTSTRAP.md` / `SOUL.md` 的默认设定继续，不阻塞对话。",
            "",
            "默认先确认两类事：",
            "- 人格：{bootstrap_identity}",
            "- 行动方式：{bootstrap_action}",
            "",
            "如果你想改，直接对我说：",
            "- 以后叫我 {preferred_name}",
            "- 语气再松一点 / 以后详细一点",
            "- 默认查全部案件",
            "- 写入仍先确认 / 以后直接写",
            "",
            "如果你不改，我就按默认继续。需要重看这条提示可以发 `/setup`。",
        ],
    },
}

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
    prompt_override_keys: set[tuple[str, str]] = field(default_factory=set)

    @classmethod
    def _apply_workspace_overrides(
        cls,
        *,
        workspace: Path | None,
        prompts: dict[str, dict[str, Any]],
        routing: dict[str, dict[str, Any]],
        templates: dict[str, dict[str, Any]],
    ) -> set[tuple[str, str]]:
        override_keys: set[tuple[str, str]] = set()
        if workspace is None:
            return override_keys

        for filename in _OVERRIDE_FILENAMES:
            path = workspace / filename
            if not path.exists():
                continue
            payload = _load_override_payload(path)
            prompts_override = payload.get("prompts")
            routing_override = payload.get("routing")
            templates_override = payload.get("templates")
            if isinstance(prompts_override, dict):
                for group, entries in prompts_override.items():
                    if isinstance(entries, dict):
                        override_keys.update((str(group), str(key)) for key in entries.keys())
                _deep_merge_dict(prompts, prompts_override)
            if isinstance(routing_override, dict):
                _deep_merge_dict(routing, routing_override)
            if isinstance(templates_override, dict):
                _deep_merge_dict(templates, templates_override)
        return override_keys

    @classmethod
    def load(cls, workspace: Path | None) -> "RuntimeTextCatalog":
        prompts = deepcopy(_DEFAULT_PROMPTS)
        routing = deepcopy(_DEFAULT_ROUTING)
        templates = deepcopy(_DEFAULT_TEMPLATES)
        override_keys = cls._apply_workspace_overrides(
            workspace=workspace,
            prompts=prompts,
            routing=routing,
            templates=templates,
        )
        return cls(
            prompts=prompts,
            routing=routing,
            templates=templates,
            prompt_override_keys=override_keys,
        )

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

    def has_prompt_override(self, group: str, key: str) -> bool:
        return (group, key) in self.prompt_override_keys
