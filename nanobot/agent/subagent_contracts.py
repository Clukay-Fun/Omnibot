"""Structured result contracts for specialist subagents."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SubagentResultContract:
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
