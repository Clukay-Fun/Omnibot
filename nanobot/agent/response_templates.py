"""Deterministic response templates for structured Feishu data replies."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

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
    """Rule-based intent router for deterministic template rendering."""

    _CASE_KEYWORDS = ("案件", "案号", "诉讼", "仲裁", "case")
    _CONTRACT_KEYWORDS = ("合同", "协议", "签约", "盖章", "付款条款", "contract", "ht")
    _CROSS_KEYWORDS = ("跨表", "总览", "汇总", "overview", "全局")

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

        if any(k in text for k in self._CROSS_KEYWORDS):
            reasons.insert(0, "命中跨表关键词/多源数据")
            return TemplateDecision(intent="cross_overview", template_id="cross.OVERVIEW", reasons=reasons)

        if any(k in text for k in self._CONTRACT_KEYWORDS) or self._contains_field(payloads, "合同"):
            reasons.insert(0, "命中合同关键词")
            return TemplateDecision(intent="contract_lookup", template_id="contract.HT-T1", reasons=reasons)

        if any(k in text for k in self._CASE_KEYWORDS) or self._contains_field(payloads, "案件"):
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
    """Render fixed-format response cards by template id."""

    def __init__(self, max_items: int = 5):
        self.max_items = max(1, max_items)

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
        return "\n".join([
            "【案件卡片 | case.T1】",
            f"- 案件编号: {case_no}",
            f"- 案件名称: {title}",
            f"- 客户: {client}",
            f"- 负责人: {owner}",
            f"- 状态: {status}",
            f"- 命中记录: {total}",
            f"- 详情链接: {url}",
        ])

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
        return "\n".join([
            "【合同卡片 | contract.HT-T1】",
            f"- 合同编号: {contract_no}",
            f"- 合同名称: {name}",
            f"- 对方主体: {counterparty}",
            f"- 负责人: {owner}",
            f"- 合同金额: {amount}",
            f"- 状态: {status}",
            f"- 签订日期: {sign_date}",
            f"- 命中记录: {total}",
            f"- 详情链接: {url}",
        ])

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

        if not source_lines:
            source_lines = ["- 数据源: —"]

        if len(preview_lines) > self.max_items:
            omitted = len(preview_lines) - self.max_items
            preview_lines = preview_lines[: self.max_items] + [f"- ... 其余 {omitted} 条请回复“下一页”"]

        return "\n".join([
            "【跨表总览 | cross.OVERVIEW】",
            "- 数据来源统计:",
            *source_lines,
            "- 结果预览:",
            *(preview_lines or ["- —"]),
        ])

    def _render_generic_summary(self, payloads: list[ToolPayload]) -> str:
        total = _total_hits(payloads)
        sources = sorted({p.tool_name for p in payloads})
        return "\n".join([
            "【摘要 | generic.SUMMARY】",
            f"- 数据来源: {', '.join(sources) if sources else '—'}",
            f"- 命中总数: {total}",
            "- 下一步建议: 请补充更具体的关键词，或回复“查看详情 <record_id>”。",
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
    return f"{tool_name}: 已获取 {count} 条结果，正在生成卡片..."


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
