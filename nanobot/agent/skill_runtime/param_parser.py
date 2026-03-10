"""描述:
主要功能:
    - 解析技能调用文本中的键值参数与查询内容。
"""

from __future__ import annotations

import re
from typing import Any

#region 参数解析逻辑

class SkillSpecParamParser:
    """
    用处: 参数解析器类，提供基于正则的键值对匹配与文本洗脱逻辑。

    功能:
        - 负责处理用户输入文本，找出 key=value 形式的数据。
        - 管理解析过程中的字符跨度消除，并填补默认值参数。
    """
    _KEY_VALUE_RE = re.compile(r"([a-zA-Z_][\w\-]*)\s*=\s*(\"[^\"]*\"|'[^']*'|\S+)")
    _NATURAL_LIMIT_PATTERNS = (
        re.compile(r"(?:列出|显示|返回|给我|只要|只看|前|top)\s*(\d{1,3})\s*(?:条|个|项|行|条记录|records?)", re.IGNORECASE),
        re.compile(r"(\d{1,3})\s*(?:条|个|项|行)\s*(?:就行|即可|够了|就好|给我)?", re.IGNORECASE),
    )

    def parse(self, text: str, *, param_schema: dict[str, Any]) -> dict[str, Any]:
        """
        用处: 执行完整的文本参数解析。参数 text: 输入文本，param_schema: 预期参数的 JSON Schema 定义。

        功能:
            - 从文本中摘取满足正则表达式的键值对并作类型转换。
            - 移除已被成功解析的部分文本，将余下信息视为查询关键字填充到特定的属性。
            - 为未提供的字段填补默认值。
        """
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
        remainder, inferred_limit = self._extract_natural_limit(remainder)

        if inferred_limit is not None:
            inferred_limit = self._clamp_limit_to_schema(inferred_limit, properties)
            if "page_size" in properties and "page_size" not in params:
                params["page_size"] = inferred_limit
            if "limit" in properties and "limit" not in params:
                params["limit"] = inferred_limit

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
        """
        用处: 从参数 Schema 中抽取有效属性字典。参数 param_schema: Schema 定义。

        功能:
            - 安全地提取出 properties 节点，如缺失则返回空字典。
        """
        properties = param_schema.get("properties")
        if isinstance(properties, dict):
            return properties
        return {}

    @staticmethod
    def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
        """
        用处: 删除字符串中被解析走的内容区段。参数 text: 原文本，spans: 被占据的子串起止索引列表。

        功能:
            - 重组并剪切字符串，剔除指定的多段下标范围内容，清理多余空白符。
        """
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
        """
        用处: 对解析出的原始字符串作弱类型转换尝试。参数 value: 原始字符串。

        功能:
            - 判断并尝试将特定的文本值转换回布尔型（True/False）或整型（int）。
        """
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

    @classmethod
    def _extract_natural_limit(cls, text: str) -> tuple[str, int | None]:
        if not text:
            return text, None
        for pattern in cls._NATURAL_LIMIT_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            try:
                limit = int(match.group(1))
            except (TypeError, ValueError):
                continue
            cleaned = cls._remove_spans(text, [match.span()]).strip(" ，,。；;:：")
            cleaned = re.sub(r"(?:请)?给我$", "", cleaned).strip(" ，,。；;:：")
            cleaned = re.sub(r"(?:就行|即可|就好|够了)$", "", cleaned).strip(" ，,。；;:：")
            return cleaned, limit
        return text, None

    @staticmethod
    def _clamp_limit_to_schema(limit: int, properties: dict[str, Any]) -> int:
        limit = max(1, limit)
        for name in ("page_size", "limit"):
            schema = properties.get(name)
            if not isinstance(schema, dict):
                continue
            maximum = schema.get("maximum")
            if isinstance(maximum, int):
                limit = min(limit, maximum)
        return limit

#endregion
