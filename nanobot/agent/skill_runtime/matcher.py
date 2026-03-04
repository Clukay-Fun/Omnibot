"""Deterministic skillspec matcher and selector."""

from __future__ import annotations

import re
from dataclasses import dataclass

from nanobot.agent.skill_runtime.spec_schema import SkillSpec


@dataclass(slots=True)
class MatchSelection:
    spec_id: str
    remainder: str
    reason: str


class SkillSpecMatcher:
    def __init__(self, specs: dict[str, SkillSpec]):
        self._specs = specs

    def select(self, text: str) -> MatchSelection | None:
        content = text.strip()
        if not content:
            return None

        explicit = self._select_explicit(content)
        if explicit:
            return explicit

        by_regex = self._select_regex(content)
        if by_regex:
            return by_regex

        return self._select_by_keywords(content)

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
                return MatchSelection(spec_id=spec_id, remainder=text, reason="regex")
        return None

    def _select_by_keywords(self, text: str) -> MatchSelection | None:
        tokens = {token for token in re.findall(r"[\w\u4e00-\u9fff]+", text.lower()) if token}
        if not tokens:
            return None

        best_id: str | None = None
        best_score = 0
        for spec_id, spec in self._specs.items():
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

        if not best_id:
            return None
        return MatchSelection(spec_id=best_id, remainder=text, reason="keywords")
