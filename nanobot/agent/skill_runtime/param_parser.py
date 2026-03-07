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

#endregion
