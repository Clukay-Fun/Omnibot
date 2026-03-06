"""Deterministic skillspec matcher and selector."""

from __future__ import annotations

import re
from dataclasses import dataclass

from nanobot.agent.skill_runtime.embedding_router import EmbeddingSkillRouter
from nanobot.agent.skill_runtime.spec_schema import SkillSpec


@dataclass(slots=True)
class MatchSelection:
    spec_id: str
    remainder: str
    reason: str
    score: float | None = None


class SkillSpecMatcher:
    _CASE_SEARCH_SPEC_ID = "case_search"

    def __init__(
        self,
        specs: dict[str, SkillSpec],
        *,
        embedding_router: EmbeddingSkillRouter | None = None,
        embedding_min_score: float = 0.15,
        case_query_keywords: tuple[str, ...] | None = None,
        case_query_intent_tokens: tuple[str, ...] | None = None,
        case_query_exclude_tokens: tuple[str, ...] | None = None,
        case_query_prefixes: tuple[str, ...] | None = None,
        case_query_suffixes: tuple[str, ...] | None = None,
    ):
        self._specs = specs
        self._embedding_router = embedding_router
        self._embedding_min_score = max(0.0, float(embedding_min_score))
        self._case_query_keywords = tuple(case_query_keywords or ("case",))
        self._case_query_intent_tokens = tuple(
            case_query_intent_tokens
            or (
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
            )
        )
        self._case_query_exclude_tokens = tuple(
            case_query_exclude_tokens
            or (
                "代办",
                "待办",
                "清单",
                "勾选",
                "卡片",
                "记一下",
                "记录",
            )
        )
        self._case_query_prefixes = tuple(case_query_prefixes or ())
        self._case_query_suffixes = tuple(case_query_suffixes or ())

    def select(self, text: str) -> MatchSelection | None:
        content = text.strip()
        if not content:
            return None

        explicit = self._select_explicit(content)
        if explicit:
            return explicit

        by_domain_hint = self._select_domain_hint(content)
        if by_domain_hint:
            return by_domain_hint

        by_regex = self._select_regex(content)
        if by_regex:
            return by_regex

        return self._select_by_keywords(content)

    def _select_domain_hint(self, text: str) -> MatchSelection | None:
        case_skill = self._CASE_SEARCH_SPEC_ID
        if case_skill in self._specs and self._looks_like_case_query(text):
            return MatchSelection(
                spec_id=case_skill,
                remainder=self._extract_case_query(text),
                reason="domain_hint",
            )
        return None

    def _looks_like_case_query(self, text: str) -> bool:
        lowered = text.lower()
        if any(token and token.lower() in lowered for token in self._case_query_exclude_tokens):
            return False
        has_object = any(keyword and keyword.lower() in lowered for keyword in self._case_query_keywords)
        if not has_object:
            return False
        has_intent = any(token and token.lower() in lowered for token in self._case_query_intent_tokens)
        if not has_intent:
            return False
        return self._has_meaningful_case_query(text)

    def _has_meaningful_case_query(self, text: str) -> bool:
        extracted = self._extract_case_query(text)
        if not extracted:
            return False
        normalized = extracted.lower()
        for token in sorted(self._case_query_keywords, key=len, reverse=True):
            lowered = token.strip().lower()
            if lowered:
                normalized = normalized.replace(lowered, "")
        normalized = re.sub(r"[\s:：\-_/，,。！？!?的]", "", normalized)
        return bool(normalized)

    def _allow_case_search_match(self, *, spec_id: str, text: str) -> bool:
        if spec_id != self._CASE_SEARCH_SPEC_ID:
            return True
        return self._looks_like_case_query(text)

    def _extract_case_query(self, text: str) -> str:
        segment = re.split(r"[，,。！？!?\n]", text.strip(), maxsplit=1)[0].strip()
        if self._case_query_prefixes:
            prefix_pattern = "|".join(re.escape(token) for token in self._case_query_prefixes if token)
            if prefix_pattern:
                segment = re.sub(rf"^(?:{prefix_pattern})\s*", "", segment)
        if self._case_query_suffixes:
            suffix_pattern = "|".join(re.escape(token) for token in self._case_query_suffixes if token)
            if suffix_pattern:
                segment = re.sub(rf"(?:的)?(?:{suffix_pattern})\s*$", "", segment)
        segment = re.sub(r"\s+", " ", segment).strip()
        return segment

    def _select_explicit(self, text: str) -> MatchSelection | None:
        match = re.match(r"^/skill\s+([a-zA-Z0-9_\-]+)\s*(.*)$", text, re.IGNORECASE)
        if not match:
            return None
        spec_id = match.group(1).strip()
        if spec_id not in self._specs:
            return None
        return MatchSelection(spec_id=spec_id, remainder=match.group(2).strip(), reason="explicit")

    def _select_regex(self, text: str) -> MatchSelection | None:
        for spec_id, spec in self._specs.items():
            regex = None
            if spec.meta.match and spec.meta.match.regex:
                regex = spec.meta.match.regex
            elif spec.match and spec.match.regex:
                regex = spec.match.regex
            if not regex:
                continue
            if re.search(regex, text, flags=re.IGNORECASE):
                if not self._allow_case_search_match(spec_id=spec_id, text=text):
                    continue
                return MatchSelection(spec_id=spec_id, remainder=text, reason="regex")
        return None

    def _select_by_keywords(self, text: str) -> MatchSelection | None:
        tokens = {token for token in re.findall(r"[\w\u4e00-\u9fff]+", text.lower()) if token}
        if not tokens:
            return self._select_by_embedding(text)

        best_id: str | None = None
        best_score = 0
        for spec_id, spec in self._specs.items():
            if not self._allow_case_search_match(spec_id=spec_id, text=text):
                continue
            score = 0
            description = (spec.meta.description or "").lower()
            for token in tokens:
                if token in description:
                    score += 2

            for part in re.split(r"[_\-]", spec_id.lower()):
                if part and part in tokens:
                    score += 3

            extra_keywords: list[str] = []
            if spec.meta.match:
                extra_keywords.extend(spec.meta.match.keywords)
            if spec.match:
                extra_keywords.extend(spec.match.keywords)
            for keyword in extra_keywords:
                if keyword and keyword.lower() in text.lower():
                    score += 4

            if score > best_score:
                best_score = score
                best_id = spec_id

        if best_id and best_score > 0:
            return MatchSelection(spec_id=best_id, remainder=text, reason="keywords")

        return self._select_by_embedding(text)

    def _select_by_embedding(self, text: str) -> MatchSelection | None:
        if not self._embedding_router:
            return None
        ranked = self._embedding_router.rank(text, self._specs)
        if not ranked:
            return None

        selected: tuple[str, float] | None = None
        for spec_id, score in ranked:
            if spec_id not in self._specs:
                continue
            if not self._allow_case_search_match(spec_id=spec_id, text=text):
                continue
            selected = (spec_id, score)
            break
        if selected is None:
            return None

        spec_id, score = selected
        if score < self._embedding_min_score:
            return None
        return MatchSelection(spec_id=spec_id, remainder=text, reason="embedding", score=float(score))
