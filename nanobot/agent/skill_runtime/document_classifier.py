"""描述:
主要功能:
    - 基于规则和回退策略执行文档类型分类。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


#region 文档分类定义

@dataclass(slots=True)
class DocumentClassification:
    """
    用处: 承载文档分类最终结果的数据结构。

    功能:
        - 记录文档所属类型及其对应的置信度得分。
        - 说明分类命中的原因及判定来源（如基于规则匹配或其余策略）。
    """
    document_type: str
    confidence: float
    reason: str
    source: str = "rule"

#endregion

#region 分类器实现

class DocumentClassifier:
    """
    用处: 核心分类引擎，用于辨别文本属于何种特定文档类型。

    功能:
        - 维护一份内置的规则词典（如发票、合同、判决书等专属词汇）。
        - 依据命中关键词数得出推测类型，以及提供后备 LLM 路由逻辑。
    """

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
        """
        用处: 执行具体的文本内容与文件名识别逻辑。参数 text: 正文内容，filename: 可选的文件名线索，llm_fallback: 兜底的大模型判断函数。

        功能:
            - 提取文本与名称进行降级匹配检索内置规则，计算综合词频打分。
            - 满足定级阈值即返回定性结果，若低于安全界限则转交给后备 LLM 函数判断。
        """
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

#endregion
