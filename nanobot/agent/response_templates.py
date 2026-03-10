"""
描述: 面向飞书结构化数据的确定性兜底与模板渲染器。
主要功能:
    - 基于正则和命中工具（Tool Payload）分析用户查询意图。
    - 在必要时直接绕过大模型生成标准飞书卡片（如案件卡/合同卡）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from nanobot.agent.runtime_texts import RuntimeTextCatalog

_FEISHU_DATA_TOOLS = {
    "bitable_search",
    "bitable_get",
    "bitable_list_tables",
    "bitable_search_person",
    "doc_search",
}


@dataclass(slots=True)
class ToolPayload:
    tool_name: str
    payload: dict[str, Any]


@dataclass(slots=True)
class TemplateDecision:
    intent: str
    template_id: str
    reasons: list[str]


def _safe_json_loads(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"value": data}
    except (TypeError, json.JSONDecodeError):
        return {"raw": raw}


def collect_tool_payloads(messages: list[dict[str, Any]]) -> list[ToolPayload]:
    payloads: list[ToolPayload] = []
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        tool_name = str(msg.get("name") or "")
        if not tool_name:
            continue
        payloads.append(ToolPayload(tool_name=tool_name, payload=_safe_json_loads(msg.get("content") or "")))
    return payloads


def uses_structured_feishu_data(payloads: list[ToolPayload]) -> bool:
    return any(p.tool_name in _FEISHU_DATA_TOOLS for p in payloads)


class TemplateRouter:
    """
    用处: 意图判定路由器。

    功能:
        - 根据用户的 User Text 及其调用的工具参数，推断当前场景究竟是在查案件、搜合同还是跨表总览。
    """

    def __init__(self, runtime_text: RuntimeTextCatalog | None = None):
        self._runtime_text = runtime_text or RuntimeTextCatalog.load(None)
        self._case_keywords = tuple(
            self._runtime_text.routing_list("domain_hints", "template_case_keywords", ["case"])
        )
        self._contract_keywords = tuple(
            self._runtime_text.routing_list("domain_hints", "template_contract_keywords", ["contract"])
        )
        self._cross_keywords = tuple(
            self._runtime_text.routing_list("domain_hints", "template_cross_keywords", ["overview"])
        )

    def route(self, user_text: str, payloads: list[ToolPayload]) -> TemplateDecision:
        text = (user_text or "").lower()
        reasons: list[str] = []

        field_hits = self._collect_field_hits(payloads)
        if field_hits:
            reasons.append(f"命中字段: {', '.join(field_hits[:4])}")

        tool_names = [p.tool_name for p in payloads]
        unique_tools = sorted(set(tool_names))
        if unique_tools:
            reasons.append(f"使用工具: {', '.join(unique_tools)}")

        if any(k in text for k in self._cross_keywords):
            reasons.insert(0, "命中跨表关键词/多源数据")
            return TemplateDecision(intent="cross_overview", template_id="cross.OVERVIEW", reasons=reasons)

        if any(k in text for k in self._contract_keywords) or self._contains_field(payloads, "合同"):
            reasons.insert(0, "命中合同关键词")
            return TemplateDecision(intent="contract_lookup", template_id="contract.HT-T1", reasons=reasons)

        if any(k in text for k in self._case_keywords) or self._contains_field(payloads, "案件"):
            reasons.insert(0, "命中案件关键词")
            return TemplateDecision(intent="case_lookup", template_id="case.T1", reasons=reasons)

        if self._looks_like_cross_query(payloads):
            reasons.insert(0, "命中跨表关键词/多源数据")
            return TemplateDecision(intent="cross_overview", template_id="cross.OVERVIEW", reasons=reasons)

        if any(p.tool_name in _FEISHU_DATA_TOOLS for p in payloads):
            reasons.insert(0, "未知场景，走兜底摘要")
            return TemplateDecision(intent="unknown", template_id="generic.SUMMARY", reasons=reasons)

        return TemplateDecision(intent="chat", template_id="chat.PLAIN", reasons=["无结构化数据"])

    def _looks_like_cross_query(self, payloads: list[ToolPayload]) -> bool:
        table_ids: set[str] = set()
        data_tools = [p for p in payloads if p.tool_name in _FEISHU_DATA_TOOLS]
        for payload in data_tools:
            for record in _extract_records(payload.payload):
                table_id = str(record.get("table_id") or "")
                if table_id:
                    table_ids.add(table_id)
            for table in payload.payload.get("tables", []) if isinstance(payload.payload.get("tables"), list) else []:
                table_id = str(table.get("table_id") or "")
                if table_id:
                    table_ids.add(table_id)
        return len(table_ids) > 1 or len({p.tool_name for p in data_tools}) > 1

    def _collect_field_hits(self, payloads: list[ToolPayload]) -> list[str]:
        hits: set[str] = set()
        for payload in payloads:
            for record in _extract_records(payload.payload):
                fields = record.get("fields")
                if isinstance(fields, dict):
                    hits.update(str(k) for k in fields.keys())
        return sorted(hits)

    def _contains_field(self, payloads: list[ToolPayload], keyword: str) -> bool:
        kw = keyword.lower()
        for payload in payloads:
            for field in self._collect_field_hits([payload]):
                if kw in field.lower():
                    return True
        return False


class TemplateRenderer:
    """
    用处: 卡片模板组装中心。

    功能:
        - 按照确定的 Template ID 与填充变量，渲染生成多维表格记录卡片文本格式。
    """

    def __init__(self, max_items: int = 5, runtime_text: RuntimeTextCatalog | None = None):
        self.max_items = max(1, max_items)
        self._runtime_text = runtime_text or RuntimeTextCatalog.load(None)

    def render(
        self,
        decision: TemplateDecision,
        payloads: list[ToolPayload],
        fallback_text: str | None,
    ) -> str:
        if decision.template_id == "case.T1":
            return self._render_case_card(payloads)
        if decision.template_id == "contract.HT-T1":
            return self._render_contract_card(payloads)
        if decision.template_id == "cross.OVERVIEW":
            return self._render_cross_overview(payloads)
        if decision.template_id == "generic.SUMMARY":
            return self._render_generic_summary(payloads)
        return fallback_text or "—"

    def _render_case_card(self, payloads: list[ToolPayload]) -> str:
        record = _first_record(payloads)
        fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
        case_no = _pick_field(fields, "案件编号", "案号", "Case ID", "ID", "编号")
        title = _pick_field(fields, "案件名称", "标题", "Case Title", "名称", "项目名称")
        client = _pick_field(fields, "客户", "委托人", "甲方", "Client")
        owner = _pick_field(fields, "负责人", "经办人", "承办人", "Owner", "主办律师")
        status = _pick_field(fields, "状态", "进度", "Status")
        url = record.get("record_url") or "—"
        total = _total_hits(payloads)
        template = self._runtime_text.template("card_case")
        header = str(template.get("header") or "")
        lines_cfg = template.get("lines") if isinstance(template.get("lines"), list) else []
        values = {
            "case_no": case_no,
            "title": title,
            "client": client,
            "owner": owner,
            "status": status,
            "total": total,
            "url": url,
        }
        lines = [str(line).format(**values) for line in lines_cfg if str(line).strip()]
        return "\n".join([header, *lines]) if header else "\n".join(lines)

    def _render_contract_card(self, payloads: list[ToolPayload]) -> str:
        record = _first_record(payloads)
        fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
        contract_no = _pick_field(fields, "合同编号", "编号", "Contract ID", "HT编号")
        name = _pick_field(fields, "合同名称", "协议名称", "名称", "Contract Name", "标题")
        counterparty = _pick_field(fields, "乙方", "对方主体", "签约方", "Counterparty")
        owner = _pick_field(fields, "负责人", "经办人", "Owner")
        amount = _pick_field(fields, "合同金额", "金额", "预算", "Amount")
        status = _pick_field(fields, "状态", "审批状态", "Status")
        sign_date = _pick_field(fields, "签订日期", "签约日期", "生效日期", "Sign Date")
        url = record.get("record_url") or "—"
        total = _total_hits(payloads)
        template = self._runtime_text.template("card_contract")
        header = str(template.get("header") or "")
        lines_cfg = template.get("lines") if isinstance(template.get("lines"), list) else []
        values = {
            "contract_no": contract_no,
            "name": name,
            "counterparty": counterparty,
            "owner": owner,
            "amount": amount,
            "status": status,
            "sign_date": sign_date,
            "total": total,
            "url": url,
        }
        lines = [str(line).format(**values) for line in lines_cfg if str(line).strip()]
        return "\n".join([header, *lines]) if header else "\n".join(lines)

    def _render_cross_overview(self, payloads: list[ToolPayload]) -> str:
        source_lines = []
        preview_lines = []
        for payload in payloads:
            if payload.tool_name not in _FEISHU_DATA_TOOLS:
                continue
            count, preview = _summarize_payload(payload.payload, self.max_items)
            source_lines.append(f"- {payload.tool_name}: {count}")
            if preview:
                preview_lines.extend(preview)

        template = self._runtime_text.template("card_overview")
        empty_source = str(template.get("empty_source") or "- —")
        overflow_hint = str(template.get("overflow_hint") or "")
        continuation = self._runtime_text.routing_list("pagination_triggers", "continuation_commands", ["next"])
        continue_command = continuation[0] if continuation else "next"

        if not source_lines:
            source_lines = [empty_source]

        if len(preview_lines) > self.max_items:
            omitted = len(preview_lines) - self.max_items
            hint = overflow_hint.format(omitted=omitted, continue_command=continue_command) if overflow_hint else ""
            preview_lines = preview_lines[: self.max_items] + ([hint] if hint else [])

        return "\n".join([
            str(template.get("header") or ""),
            str(template.get("source_title") or ""),
            *source_lines,
            str(template.get("preview_title") or ""),
            *(preview_lines or [str(template.get("empty_preview") or "- —")]),
        ])

    def _render_generic_summary(self, payloads: list[ToolPayload]) -> str:
        total = _total_hits(payloads)
        sources = sorted({p.tool_name for p in payloads})
        template = self._runtime_text.template("card_summary")
        source_text = ", ".join(sources) if sources else "—"
        return "\n".join([
            str(template.get("header") or ""),
            str(template.get("source_line") or "").format(sources=source_text),
            str(template.get("total_line") or "").format(total=total),
            str(template.get("next_step") or ""),
        ])


def build_audit_summary(
    decision: TemplateDecision,
    payloads: list[ToolPayload],
    timings: list[dict[str, Any]],
    total_ms: int,
) -> str:
    sources = []
    for payload in payloads:
        if payload.tool_name not in _FEISHU_DATA_TOOLS:
            continue
        count, _ = _summarize_payload(payload.payload, max_items=1)
        sources.append(f"{payload.tool_name}({count})")

    timing_parts = []
    for item in timings[:8]:
        stage = str(item.get("stage") or "step")
        ms = int(item.get("duration_ms") or 0)
        timing_parts.append(f"{stage} {ms}ms")

    return "\n".join([
        "[可审计摘要]",
        f"- 意图: {decision.intent}",
        f"- 模板: {decision.template_id}",
        f"- 数据来源: {', '.join(sources) if sources else '—'}",
        f"- 判定依据: {'; '.join(decision.reasons) if decision.reasons else '—'}",
        f"- 耗时: 总计 {total_ms}ms | {'; '.join(timing_parts) if timing_parts else '无步骤耗时'}",
    ])


def build_quick_progress(tool_name: str, result: str) -> str | None:
    if tool_name not in _FEISHU_DATA_TOOLS:
        return None
    payload = _safe_json_loads(result)
    count, _ = _summarize_payload(payload, max_items=0)
    if count == "—":
        return None
    return f"{tool_name}: 已获取 {count} 条结果"


def _extract_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("record"), dict):
        return [payload["record"]]
    if isinstance(payload.get("records"), list):
        return [r for r in payload["records"] if isinstance(r, dict)]
    return []


def _first_record(payloads: list[ToolPayload]) -> dict[str, Any]:
    for payload in payloads:
        records = _extract_records(payload.payload)
        if records:
            return records[0]
    return {"fields": {}}


def _pick_field(fields: dict[str, Any], *candidates: str) -> str:
    lowered = {str(k).lower(): v for k, v in fields.items()}
    for candidate in candidates:
        if candidate in fields:
            return _as_text(fields[candidate])
        value = lowered.get(candidate.lower())
        if value is not None:
            return _as_text(value)
    for key, value in fields.items():
        key_text = str(key)
        if any(_normalize(candidate) in _normalize(key_text) for candidate in candidates):
            return _as_text(value)
    return "—"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def _as_text(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, dict):
                items.append(str(item.get("name") or item.get("id") or item))
            else:
                items.append(str(item))
        return ", ".join(i for i in items if i) or "—"
    if isinstance(value, dict):
        if "name" in value:
            return str(value["name"])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _total_hits(payloads: list[ToolPayload]) -> int:
    total = 0
    for payload in payloads:
        raw_total = payload.payload.get("total")
        if isinstance(raw_total, int):
            total += raw_total
            continue
        total += len(_extract_records(payload.payload))
    return total


def _summarize_payload(payload: dict[str, Any], max_items: int) -> tuple[str, list[str]]:
    records = _extract_records(payload)
    if records:
        total = payload.get("total") if isinstance(payload.get("total"), int) else len(records)
        preview = []
        for record in records[:max_items]:
            fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
            title = (
                _pick_field(fields, "案件名称", "合同名称", "名称", "标题", "Name", "Title")
                if fields
                else record.get("record_id")
            )
            record_id = record.get("record_id") or "—"
            preview.append(f"- {record_id}: {title}")
        return str(total), preview

    if isinstance(payload.get("documents"), list):
        docs = [d for d in payload["documents"] if isinstance(d, dict)]
        preview = [f"- {d.get('title', '—')}" for d in docs[:max_items]]
        return str(payload.get("total") if isinstance(payload.get("total"), int) else len(docs)), preview

    if isinstance(payload.get("tables"), list):
        tables = [t for t in payload["tables"] if isinstance(t, dict)]
        preview = [f"- {t.get('name', '—')} ({t.get('table_id', '—')})" for t in tables[:max_items]]
        return str(len(tables)), preview

    return "—", []
