"""Helpers for reading and snapshotting WORKLOG.md."""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar


class WorklogStore:
    """Read the full worklog and build a compact snapshot for prompts."""

    _SECTION_RE = re.compile(r"^##\s+(进行中|待处理|已完成)\s*$", re.MULTILINE)
    _CANONICAL_ITEM_RE = re.compile(r"^###\s+(.+?)\s*$")
    _LEGACY_ITEM_RE = re.compile(r"^(?:\d+\.)\s+(.+?)\s*$")
    _FIELD_RE = re.compile(r"^\s*-\s*([^：:]+)\s*[：:]\s*(.+?)\s*$")
    _SNAPSHOT_CHAR_LIMIT = 2500
    _TRUNCATED_MARKER = "[truncated]"
    _SECTION_ORDER: ClassVar[tuple[str, str, str]] = ("进行中", "待处理", "已完成")
    _PLACEHOLDER_TITLES: ClassVar[set[str]] = {"事项标题"}
    _PLACEHOLDER_STATUS_SNIPPETS: ClassVar[tuple[str, ...]] = (
        "一句话描述当前状态和下一个可执行动作",
        "已完成；如有必要，可补一句收尾说明",
    )
    _HEADER = """# WORKLOG.md - 当前工作面板

这里记录当前值得持续追踪的工作事项。

规则：
- 只记录有明确下一步、可推进、可完成的事项
- `进行中` + `待处理` 合计最多保留 7 项，按优先级和当前重要性排序
- `已完成` 只保留最近 5 项
- 需要待确认时，直接写进 `状态/下一步`
- 可延期事项放在 `待处理` 且优先级设为 `低`
"""
    _SECTION_COMMENTS = {
        "进行中": """<!-- 正在推进的事项。默认第一项视为现在最值得先做。 -->
<!-- 格式：
### 事项标题
- 优先级：高/中/低
- 状态/下一步：一句话描述当前状态和下一个可执行动作
不要添加阻塞、进展、截止日期等其他字段。
-->""",
        "待处理": """<!-- 已知要做但尚未开始的事项。 -->
<!-- 格式：
### 事项标题
- 优先级：高/中/低
- 状态/下一步：一句话描述当前状态和下一个可执行动作
不要添加阻塞、进展、截止日期等其他字段。
-->""",
        "已完成": """<!-- 最近完成的事项。仅保留最近 5 项。 -->
<!-- 格式：
### 事项标题
- 优先级：高/中/低
- 状态/下一步：已完成；如有必要，可补一句收尾说明
不要添加阻塞、进展、截止日期等其他字段。
-->""",
    }

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.worklog_file = workspace / "WORKLOG.md"

    def read_full(self) -> str:
        if self.worklog_file.exists():
            return self.worklog_file.read_text(encoding="utf-8")
        return ""

    def build_snapshot(self) -> str:
        """Return a compact worklog snapshot safe for prompt injection."""
        content = self.read_full().strip()
        if not content:
            return ""

        sections = self._parse_content(content)
        if sections is None:
            return self._truncate(content)

        sections = self._dedupe_and_trim(sections)
        in_progress = sections["进行中"][:5]
        pending = sections["待处理"][:3]
        parts: list[str] = []

        if in_progress:
            parts.append("## 进行中\n\n" + "\n\n".join(self._render_item(item) for item in in_progress))
        if pending:
            parts.append("## 待处理\n\n" + "\n\n".join(self._render_item(item) for item in pending))
        if not parts:
            return "暂无活跃工作事项。"

        return self._truncate("\n\n".join(parts))

    @classmethod
    def normalize_content(cls, content: str) -> str:
        """Normalize a worklog to the canonical schema when possible."""
        text = content.strip()
        if not text:
            return text

        sections = cls._parse_content(text)
        if sections is None:
            return text
        return cls._render_worklog(cls._dedupe_and_trim(sections))

    @classmethod
    def _parse_content(cls, content: str) -> dict[str, list[dict[str, str]]] | None:
        matches = list(cls._SECTION_RE.finditer(content))
        if not matches:
            return None

        sections: dict[str, str] = {}
        for idx, match in enumerate(matches):
            title = match.group(1)
            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
            sections[title] = content[start:end].strip()

        required = ("进行中", "待处理", "已完成")
        if any(name not in sections for name in required):
            return None

        parsed: dict[str, list[dict[str, str]]] = {}
        for title, body in sections.items():
            parsed[title] = cls._parse_items(title, body)
        return parsed

    @classmethod
    def _parse_items(cls, section: str, body: str) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        current_title: str | None = None
        fields: dict[str, str | list[str]] = {}

        def _flush() -> None:
            nonlocal current_title, fields
            if not current_title:
                return
            items.append(
                {
                    "title": current_title.strip(),
                    "priority": cls._normalize_priority(fields.get("priority")),
                    "status": cls._compose_status(section, fields),
                }
            )
            current_title = None
            fields = {}

        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("<!--"):
                continue

            canonical = cls._CANONICAL_ITEM_RE.match(line)
            legacy = cls._LEGACY_ITEM_RE.match(line)
            if canonical or legacy:
                _flush()
                current_title = (canonical or legacy).group(1)
                continue

            field = cls._FIELD_RE.match(raw_line)
            if current_title and field:
                key = field.group(1).strip()
                value = field.group(2).strip()
                cls._consume_field(fields, key, value)

        _flush()
        return items

    def _truncate(self, content: str) -> str:
        text = content.strip()
        if len(text) <= self._SNAPSHOT_CHAR_LIMIT:
            return text

        budget = self._SNAPSHOT_CHAR_LIMIT - len(self._TRUNCATED_MARKER) - 2
        if budget <= 0:
            return self._TRUNCATED_MARKER
        return text[:budget].rstrip() + "\n\n" + self._TRUNCATED_MARKER

    @classmethod
    def _consume_field(cls, fields: dict[str, str | list[str]], key: str, value: str) -> None:
        normalized = key.replace(" ", "").strip()
        if normalized == "优先级":
            fields["priority"] = value
            return
        if normalized == "状态/下一步":
            fields["status_next"] = value
            return
        if normalized == "状态":
            fields["status"] = value
            return
        if normalized == "下一步":
            fields["next_step"] = value
            return
        if normalized == "进展":
            fields["progress"] = value
            return
        if normalized == "阻塞":
            fields["blocker"] = value
            return
        fields.setdefault("extras", [])
        fields["extras"].append(f"{key}：{value}")

    @staticmethod
    def _normalize_priority(value: str | None) -> str:
        text = (value or "").strip()
        if "高" in text:
            return "高"
        if "低" in text:
            return "低"
        return "中"

    @classmethod
    def _compose_status(cls, section: str, fields: dict[str, str | list[str]]) -> str:
        parts: list[str] = []
        status_next = fields.get("status_next")
        if status_next:
            parts.append(str(status_next))
        else:
            status = fields.get("status")
            next_step = fields.get("next_step")
            if status:
                parts.append(str(status))
            if next_step:
                parts.append(f"下一步：{next_step}")

        progress = fields.get("progress")
        if progress:
            parts.append(f"进展：{progress}")

        blocker = fields.get("blocker")
        if blocker and blocker not in {"无", "暂无", "none", "None", "无明显阻塞"}:
            parts.append(f"阻塞：{blocker}")

        extras = fields.get("extras", [])
        if isinstance(extras, list):
            parts.extend(str(item) for item in extras)

        if parts:
            return "；".join(parts)
        if section == "已完成":
            return "已完成"
        return "待补充下一步"

    @classmethod
    def _dedupe_and_trim(cls, sections: dict[str, list[dict[str, str]]]) -> dict[str, list[dict[str, str]]]:
        occurrences: list[tuple[str, str, dict[str, str]]] = []
        for section in cls._SECTION_ORDER:
            for item in sections.get(section, []):
                if cls._is_placeholder_item(item):
                    continue
                key = cls._normalize_title(item["title"])
                occurrences.append((section, key, item))

        last_index = {key: idx for idx, (_section, key, _item) in enumerate(occurrences)}
        deduped = {section: [] for section in cls._SECTION_ORDER}
        for idx, (section, key, item) in enumerate(occurrences):
            if last_index[key] == idx:
                deduped[section].append(item)

        deduped["已完成"] = deduped["已完成"][:5]
        deduped["进行中"] = deduped["进行中"][:7]
        remaining = max(0, 7 - len(deduped["进行中"]))
        deduped["待处理"] = deduped["待处理"][:remaining]
        return deduped

    @classmethod
    def _is_placeholder_item(cls, item: dict[str, str]) -> bool:
        title = item.get("title", "").strip()
        if title in cls._PLACEHOLDER_TITLES:
            return True
        status = item.get("status", "").strip()
        return any(snippet in status for snippet in cls._PLACEHOLDER_STATUS_SNIPPETS)

    @staticmethod
    def _normalize_title(title: str) -> str:
        return re.sub(r"\s+", "", title).strip().lower()

    @classmethod
    def _render_worklog(cls, sections: dict[str, list[dict[str, str]]]) -> str:
        parts = [cls._HEADER.strip()]
        for section in cls._SECTION_ORDER:
            parts.append(f"## {section}")
            parts.append(cls._SECTION_COMMENTS[section])
            items = sections.get(section, [])
            if items:
                parts.append("\n\n".join(cls._render_item(item) for item in items))
        return "\n\n".join(part.rstrip() for part in parts if part).strip() + "\n"

    @staticmethod
    def _render_item(item: dict[str, str]) -> str:
        return (
            f"### {item['title']}\n"
            f"- 优先级：{item['priority']}\n"
            f"- 状态/下一步：{item['status']}"
        )
