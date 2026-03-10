"""
描述: 垂直类微代理的数据返回契约定义中心。
主要功能:
    - 统一定义并校验各种执行计划（Plan/Research/Apply）结束后，LLM 返回结构的形式是否处于合法 JSON。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SubagentResultContract:
    """
    用处: 子代理与主路由器的消息约定协议。

    功能:
        - 确保 JSON 化文本的确定性转义格式。包裹并提取 AI 模型自我判定的置信度、成功状态以及建议的后续接力动作。
    """
    kind: str
    status: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    confidence: str = "medium"
    next_action: str = "report"
    raw_text: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "kind": self.kind,
            "status": self.status,
            "summary": self.summary,
            "data": self.data,
            "confidence": self.confidence,
            "next_action": self.next_action,
        }
        if self.raw_text:
            payload["raw_text"] = self.raw_text
        return payload


def normalize_subagent_contract(
    raw_result: str | None,
    *,
    mode: str,
    status: str = "ok",
) -> SubagentResultContract:
    text = str(raw_result or "").strip()
    if text:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            summary = str(parsed.get("summary") or parsed.get("message") or "").strip() or text
            data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
            return SubagentResultContract(
                kind=str(parsed.get("kind") or mode or "subagent_result").strip() or "subagent_result",
                status=str(parsed.get("status") or status or "ok").strip() or "ok",
                summary=summary,
                data=dict(data),
                confidence=str(parsed.get("confidence") or "medium").strip() or "medium",
                next_action=str(parsed.get("next_action") or "report").strip() or "report",
                raw_text=text,
            )

    summary = text or ("Subagent completed successfully." if status == "ok" else "Subagent failed.")
    data = {"text": summary} if text else {}
    return SubagentResultContract(
        kind=mode or "subagent_result",
        status=status or "ok",
        summary=summary,
        data=data,
        confidence="medium",
        next_action="report",
        raw_text=text,
    )
