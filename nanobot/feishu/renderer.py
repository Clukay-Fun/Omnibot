"""Feishu outbound rendering helpers."""

from __future__ import annotations

import json
import re

import mistune

from nanobot.utils.emoji import emojize_text

_POST_AST = mistune.create_markdown(renderer="ast")


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

    @classmethod
    def render_reply_post(cls, content: str) -> tuple[str, str]:
        """Render a user-triggered Feishu reply as post when possible, else text."""
        plain = emojize_text(cls.markdown_to_plain_text(content).strip())
        if len(plain) > cls._POST_MAX_LEN:
            return "text", json.dumps({"text": plain}, ensure_ascii=False)
        return "post", cls.markdown_to_post(content)

    @classmethod
    def render_final_reply(cls, content: str) -> tuple[str, str]:
        """Render a final Feishu turn reply through the explicit converter pipeline."""
        fmt = cls.detect_final_reply_format(content)
        plain = emojize_text(cls.markdown_to_plain_text(content).strip())

        if fmt == "text":
            return "text", json.dumps({"text": plain}, ensure_ascii=False)
        if fmt == "post":
            return "post", cls.markdown_to_post(content)

        elements = cls.build_card_elements(content)
        card = {
            "config": {"wide_screen_mode": True},
            "elements": elements or [{"tag": "markdown", "content": content}],
        }
        return "interactive", json.dumps(card, ensure_ascii=False)

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
    def detect_final_reply_format(cls, content: str) -> str:
        """Determine the final-reply delivery mode for a turn-bound Feishu reply."""
        stripped = content.strip()
        if not stripped:
            return "text"
        if cls._COMPLEX_MD_RE.search(stripped):
            return "interactive"
        if len(stripped) > cls._POST_MAX_LEN:
            return "interactive"
        if cls._MD_LINK_RE.search(stripped):
            return "post"
        if cls._LIST_RE.search(stripped) or cls._OLIST_RE.search(stripped):
            return "post"
        if cls._SIMPLE_MD_RE.search(stripped):
            return "post"
        plain = emojize_text(cls.markdown_to_plain_text(content).strip())
        if len(plain) <= cls._TEXT_MAX_LEN:
            return "text"
        return "post"

    @classmethod
    def markdown_to_post(cls, content: str) -> str:
        """Convert markdown content to a Feishu post payload."""
        paragraphs = cls._ast_to_post_paragraphs(cls._parse_ast(content))
        if not paragraphs:
            paragraphs = [[{"tag": "text", "text": emojize_text(content.strip())}]]
        return json.dumps({"zh_cn": {"content": paragraphs}}, ensure_ascii=False)

    @classmethod
    def markdown_to_plain_text(cls, content: str) -> str:
        """Convert markdown into readable plain text for text-message fallback."""
        blocks = cls._ast_to_plain_blocks(cls._parse_ast(content))
        plain = "\n\n".join(block for block in blocks if block.strip()).strip()
        return emojize_text(plain or content.strip())

    @staticmethod
    def _parse_ast(content: str) -> list[dict]:
        parsed = _POST_AST(content or "")
        return parsed if isinstance(parsed, list) else []

    @classmethod
    def _ast_to_post_paragraphs(cls, nodes: list[dict]) -> list[list[dict]]:
        paragraphs: list[list[dict]] = []
        for node in nodes:
            node_type = node.get("type")
            if node_type == "blank_line":
                continue
            if node_type == "heading":
                text = cls._inline_plain_text(node.get("children", []))
                if text:
                    paragraphs.append([{"tag": "text", "text": emojize_text(text)}])
                continue
            if node_type == "paragraph":
                paragraphs.append(cls._inline_post_segments(node.get("children", [])) or [{"tag": "text", "text": ""}])
                continue
            if node_type == "list":
                paragraphs.extend(cls._list_to_post_paragraphs(node))
                continue
            if node_type == "block_code":
                info = str(node.get("attrs", {}).get("info") or "").strip()
                code = str(node.get("raw") or "").rstrip("\n")
                fence = f"```{info}\n{code}\n```" if info else f"```\n{code}\n```"
                paragraphs.append([{"tag": "text", "text": emojize_text(fence)}])
                continue
            text = cls._block_plain_text(node)
            if text:
                paragraphs.append([{"tag": "text", "text": emojize_text(text)}])
        return paragraphs

    @classmethod
    def _ast_to_plain_blocks(cls, nodes: list[dict]) -> list[str]:
        blocks: list[str] = []
        for node in nodes:
            node_type = node.get("type")
            if node_type == "blank_line":
                continue
            if node_type == "heading":
                text = cls._inline_plain_text(node.get("children", []))
                if text:
                    blocks.append(text)
                continue
            if node_type == "paragraph":
                text = cls._inline_plain_text(node.get("children", []))
                if text:
                    blocks.append(text)
                continue
            if node_type == "list":
                blocks.extend(cls._list_to_plain_blocks(node))
                continue
            if node_type == "block_code":
                info = str(node.get("attrs", {}).get("info") or "").strip()
                code = str(node.get("raw") or "").rstrip("\n")
                blocks.append(f"```{info}\n{code}\n```" if info else f"```\n{code}\n```")
                continue
            text = cls._block_plain_text(node)
            if text:
                blocks.append(text)
        return blocks

    @classmethod
    def _list_to_post_paragraphs(cls, node: dict) -> list[list[dict]]:
        ordered = bool(node.get("attrs", {}).get("ordered"))
        paragraphs: list[list[dict]] = []
        for index, item in enumerate(node.get("children", []), start=1):
            prefix = f"{index}. " if ordered else "• "
            paragraphs.append([{"tag": "text", "text": emojize_text(prefix + cls._list_item_plain_text(item))}])
        return paragraphs

    @classmethod
    def _list_to_plain_blocks(cls, node: dict) -> list[str]:
        ordered = bool(node.get("attrs", {}).get("ordered"))
        blocks: list[str] = []
        for index, item in enumerate(node.get("children", []), start=1):
            prefix = f"{index}. " if ordered else "• "
            blocks.append(prefix + cls._list_item_plain_text(item))
        return blocks

    @classmethod
    def _list_item_plain_text(cls, node: dict) -> str:
        parts = [cls._block_plain_text(child) for child in node.get("children", [])]
        text = "\n".join(part for part in parts if part.strip()).strip()
        return text or cls._block_plain_text(node)

    @classmethod
    def _inline_post_segments(cls, nodes: list[dict]) -> list[dict]:
        segments: list[dict] = []
        text_buffer = ""

        def flush() -> None:
            nonlocal text_buffer
            if text_buffer:
                segments.append({"tag": "text", "text": emojize_text(text_buffer)})
                text_buffer = ""

        for node in nodes:
            node_type = node.get("type")
            if node_type == "text":
                text_buffer += str(node.get("raw") or "")
            elif node_type in {"softbreak", "linebreak"}:
                text_buffer += "\n"
            elif node_type == "link":
                flush()
                label = cls._inline_plain_text(node.get("children", [])) or str(node.get("attrs", {}).get("url") or "")
                href = str(node.get("attrs", {}).get("url") or "")
                segments.append({"tag": "a", "text": emojize_text(label), "href": href})
            elif node_type == "codespan":
                text_buffer += f"`{str(node.get('raw') or '')}`"
            elif node_type in {"strong", "emphasis", "strikethrough"}:
                text_buffer += cls._inline_plain_text(node.get("children", []))
            elif node.get("children"):
                text_buffer += cls._inline_plain_text(node.get("children", []))
            elif node.get("raw"):
                text_buffer += str(node.get("raw"))
        flush()
        return segments

    @classmethod
    def _inline_plain_text(cls, nodes: list[dict]) -> str:
        parts: list[str] = []
        for node in nodes:
            node_type = node.get("type")
            if node_type == "text":
                parts.append(str(node.get("raw") or ""))
            elif node_type in {"softbreak", "linebreak"}:
                parts.append("\n")
            elif node_type == "link":
                label = cls._inline_plain_text(node.get("children", [])) or str(node.get("attrs", {}).get("url") or "")
                href = str(node.get("attrs", {}).get("url") or "")
                parts.append(f"{label} ({href})" if href else label)
            elif node_type == "codespan":
                parts.append(f"`{str(node.get('raw') or '')}`")
            elif node.get("children"):
                parts.append(cls._inline_plain_text(node.get("children", [])))
            elif node.get("raw"):
                parts.append(str(node.get("raw")))
        return "".join(parts).strip()

    @classmethod
    def _block_plain_text(cls, node: dict) -> str:
        if node.get("children"):
            return cls._inline_plain_text(node.get("children", []))
        return str(node.get("raw") or "").strip()
