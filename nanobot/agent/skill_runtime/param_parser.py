"""Parameter parsing helpers for skillspec runtime."""

from __future__ import annotations

import re
from typing import Any


class SkillSpecParamParser:
    _KEY_VALUE_RE = re.compile(r"([a-zA-Z_][\w\-]*)\s*=\s*(\"[^\"]*\"|'[^']*'|\S+)")

    def parse(self, text: str, *, param_schema: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {}
        content = text.strip()
        if not content:
            return params

        consumed: list[tuple[int, int]] = []
        for match in self._KEY_VALUE_RE.finditer(content):
            key = match.group(1).strip()
            value = match.group(2).strip().strip("\"'")
            params[key] = self._coerce_value(value)
            consumed.append(match.span())

        remainder = self._remove_spans(content, consumed).strip()
        properties = self._properties(param_schema)
        has_query = "query" in properties
        if remainder and has_query and "query" not in params:
            params["query"] = remainder

        for name, schema in properties.items():
            if name in params:
                continue
            if isinstance(schema, dict) and "default" in schema:
                params[name] = schema["default"]

        return params

    @staticmethod
    def _properties(param_schema: dict[str, Any]) -> dict[str, Any]:
        properties = param_schema.get("properties")
        if isinstance(properties, dict):
            return properties
        return {}

    @staticmethod
    def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
        if not spans:
            return text
        out: list[str] = []
        cursor = 0
        for start, end in spans:
            out.append(text[cursor:start])
            cursor = end
        out.append(text[cursor:])
        return " ".join("".join(out).split())

    @staticmethod
    def _coerce_value(value: str) -> Any:
        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if re.fullmatch(r"-?\d+", value):
            try:
                return int(value)
            except ValueError:
                return value
        return value
