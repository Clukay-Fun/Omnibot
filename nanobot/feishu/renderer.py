"""Feishu outbound rendering helpers."""

from __future__ import annotations

import json
import re


class FeishuRenderer:
    """Render outbound content into Feishu-specific message payload pieces."""

    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )
    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    _CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)
    _COMPLEX_MD_RE = re.compile(
        r"```"
        r"|^\|.+\|.*\n\s*\|[-:\s|]+\|"
        r"|^#{1,6}\s+",
        re.MULTILINE,
    )
    _SIMPLE_MD_RE = re.compile(
        r"\*\*.+?\*\*"
        r"|__.+?__"
        r"|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"
        r"|~~.+?~~",
        re.DOTALL,
    )
    _MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
    _LIST_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)
    _OLIST_RE = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)
    _TEXT_MAX_LEN = 200
    _POST_MAX_LEN = 2000

    @staticmethod
    def parse_md_table(table_text: str) -> dict | None:
        """Parse a markdown table into a Feishu table element."""
        lines = [_line.strip() for _line in table_text.strip().split("\n") if _line.strip()]
        if len(lines) < 3:
            return None

        def split(_line: str) -> list[str]:
            return [cell.strip() for cell in _line.strip("|").split("|")]

        headers = split(lines[0])
        rows = [split(_line) for _line in lines[2:]]
        columns = [
            {"tag": "column", "name": f"c{i}", "display_name": header, "width": "auto"}
            for i, header in enumerate(headers)
        ]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [
                {f"c{i}": row[i] if i < len(row) else "" for i in range(len(headers))}
                for row in rows
            ],
        }

    @classmethod
    def build_card_elements(cls, content: str) -> list[dict]:
        """Split content into div/markdown + table elements for Feishu card."""
        elements, last_end = [], 0
        for match in cls._TABLE_RE.finditer(content):
            before = content[last_end:match.start()]
            if before.strip():
                elements.extend(cls.split_headings(before))
            elements.append(cls.parse_md_table(match.group(1)) or {"tag": "markdown", "content": match.group(1)})
            last_end = match.end()
        remaining = content[last_end:]
        if remaining.strip():
            elements.extend(cls.split_headings(remaining))
        return elements or [{"tag": "markdown", "content": content}]

    @staticmethod
    def split_elements_by_table_limit(elements: list[dict], max_tables: int = 1) -> list[list[dict]]:
        """Split card elements into groups with at most *max_tables* table elements each."""
        if not elements:
            return [[]]
        groups: list[list[dict]] = []
        current: list[dict] = []
        table_count = 0
        for element in elements:
            if element.get("tag") == "table":
                if table_count >= max_tables:
                    if current:
                        groups.append(current)
                    current = []
                    table_count = 0
                current.append(element)
                table_count += 1
            else:
                current.append(element)
        if current:
            groups.append(current)
        return groups or [[]]

    @classmethod
    def split_headings(cls, content: str) -> list[dict]:
        """Split content by headings, converting headings to div elements."""
        protected = content
        code_blocks = []
        for match in cls._CODE_BLOCK_RE.finditer(content):
            code_blocks.append(match.group(1))
            protected = protected.replace(match.group(1), f"\x00CODE{len(code_blocks)-1}\x00", 1)

        elements = []
        last_end = 0
        for match in cls._HEADING_RE.finditer(protected):
            before = protected[last_end:match.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            text = match.group(2).strip()
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{text}**",
                },
            })
            last_end = match.end()
        remaining = protected[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})

        for i, code_block in enumerate(code_blocks):
            for element in elements:
                if element.get("tag") == "markdown":
                    element["content"] = element["content"].replace(f"\x00CODE{i}\x00", code_block)

        return elements or [{"tag": "markdown", "content": content}]

    @classmethod
    def detect_msg_format(cls, content: str) -> str:
        """Determine the optimal Feishu message format for *content*."""
        stripped = content.strip()
        if cls._COMPLEX_MD_RE.search(stripped):
            return "interactive"
        if len(stripped) > cls._POST_MAX_LEN:
            return "interactive"
        if cls._SIMPLE_MD_RE.search(stripped):
            return "interactive"
        if cls._LIST_RE.search(stripped) or cls._OLIST_RE.search(stripped):
            return "interactive"
        if cls._MD_LINK_RE.search(stripped):
            return "post"
        if len(stripped) <= cls._TEXT_MAX_LEN:
            return "text"
        return "post"

    @classmethod
    def markdown_to_post(cls, content: str) -> str:
        """Convert markdown content to Feishu post message JSON."""
        lines = content.strip().split("\n")
        paragraphs: list[list[dict]] = []

        for line in lines:
            elements: list[dict] = []
            last_end = 0

            for match in cls._MD_LINK_RE.finditer(line):
                before = line[last_end:match.start()]
                if before:
                    elements.append({"tag": "text", "text": before})
                elements.append({
                    "tag": "a",
                    "text": match.group(1),
                    "href": match.group(2),
                })
                last_end = match.end()

            remaining = line[last_end:]
            if remaining:
                elements.append({"tag": "text", "text": remaining})

            if not elements:
                elements.append({"tag": "text", "text": ""})

            paragraphs.append(elements)

        return json.dumps({"zh_cn": {"content": paragraphs}}, ensure_ascii=False)
