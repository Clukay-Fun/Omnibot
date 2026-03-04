"""Rule-based document classifier with optional LLM fallback."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class DocumentClassification:
    """Classification output for a document."""

    document_type: str
    confidence: float
    reason: str
    source: str = "rule"


class DocumentClassifier:
    """Classify document type from text and filename hints."""

    _RULES: dict[str, tuple[str, ...]] = {
        "invoice": (
            "invoice",
            "tax invoice",
            "amount due",
            "bill to",
            "invoice number",
        ),
        "contract": (
            "agreement",
            "party a",
            "party b",
            "effective date",
            "term and termination",
        ),
        "judgment": (
            "judgment",
            "court",
            "plaintiff",
            "defendant",
            "case number",
        ),
    }

    def classify(
        self,
        text: str,
        *,
        filename: str | None = None,
        llm_fallback: Callable[[str, str | None], DocumentClassification | None] | None = None,
    ) -> DocumentClassification:
        """Classify by deterministic rules, then optional fallback."""
        normalized = (text or "").lower()
        if filename:
            normalized = f"{filename.lower()}\n{normalized}"

        best_type = "unknown"
        best_hits = 0
        for doc_type, keywords in self._RULES.items():
            hits = sum(1 for kw in keywords if kw in normalized)
            if hits > best_hits:
                best_type = doc_type
                best_hits = hits

        if best_hits >= 2:
            confidence = min(0.95, 0.5 + best_hits * 0.15)
            return DocumentClassification(
                document_type=best_type,
                confidence=confidence,
                reason=f"matched {best_hits} rule keywords",
                source="rule",
            )

        if llm_fallback:
            fallback = llm_fallback(text, filename)
            if fallback is not None:
                return fallback

        return DocumentClassification(
            document_type="unknown",
            confidence=0.1,
            reason="no strong rule match; fallback unavailable",
            source="rule",
        )
