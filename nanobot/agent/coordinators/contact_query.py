"""Coordinator for high-certainty directory/contact queries."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from nanobot.agent.coordinators.base import AgentCoordinator
from nanobot.agent.pending_write import extract_json_object
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.session.manager import Session

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop


_LIST_TOKENS = ("通讯录", "联系人", "同事")
_LIST_PROMPTS = ("都有谁", "有哪些", "哪些人", "列一下", "列出", "名单", "所有人")
_WRITE_TOKENS = ("新增", "创建", "写入", "添加", "记到", "记录到", "更新", "修改", "删除", "移除")
_NON_DIRECTORY_KEYWORDS = (
    "日程",
    "任务",
    "待办",
    "会议",
    "日历",
    "提醒",
    "表格",
    "多维表格",
    "记录",
    "字段",
    "schema",
    "视图",
    "文档",
    "云文档",
    "文件",
    "消息",
    "聊天记录",
    "历史消息",
)
_LOOKUP_RE = re.compile(
    r"^\s*(?:帮我)?(?:查|找|搜)(?:一下)?\s*(?P<keyword>[\w.@\-\u4e00-\u9fff]{2,40})\s*$",
    re.IGNORECASE,
)


class ContactQueryCoordinator(AgentCoordinator):
    _PAGE_SIZE = 5

    def __init__(self, agent: AgentLoop) -> None:
        super().__init__(agent)

    @property
    def _loop(self) -> AgentLoop:
        assert self._agent is not None
        return self._agent

    @staticmethod
    def _record_direct_turn(session: Session, msg: InboundMessage, assistant_content: str) -> None:
        session.add_message("user", msg.content)
        session.add_message("assistant", assistant_content)

    @staticmethod
    def _store_recent_hits(session: Session, *, keyword: str | None, contacts: list[dict[str, Any]]) -> None:
        metadata = dict(session.metadata or {})
        metadata["recent_directory_hits"] = [dict(item) for item in contacts]
        metadata["recent_directory_query"] = keyword or ""
        metadata["recent_directory_offset"] = min(len(contacts), ContactQueryCoordinator._PAGE_SIZE)
        metadata["result_selection"] = {
            "kind": "directory_contacts",
            "items": [dict(item) for item in contacts],
            "offset": min(len(contacts), ContactQueryCoordinator._PAGE_SIZE),
            "page_size": ContactQueryCoordinator._PAGE_SIZE,
            "query": keyword or "",
        }
        session.metadata = metadata

    @staticmethod
    def _directory_intent(msg: InboundMessage) -> tuple[str | None, int] | None:
        if msg.channel != "feishu":
            return None
        text = msg.content.strip()
        if not text or any(token in text for token in _WRITE_TOKENS):
            return None
        if any(token in text for token in _LIST_TOKENS) and any(token in text for token in _LIST_PROMPTS):
            return None, 10
        match = _LOOKUP_RE.match(text)
        if match:
            keyword = str(match.group("keyword") or "").strip()
            if any(token in keyword for token in _NON_DIRECTORY_KEYWORDS):
                return None
            return keyword, 5
        return None

    @staticmethod
    def _format_contacts(keyword: str | None, contacts: list[dict[str, Any]], *, has_more: bool = False) -> str:
        if not contacts:
            if keyword:
                return f"通讯录里暂时没找到“{keyword}”。"
            return "通讯录里暂时没有可展示的联系人。"

        if keyword:
            lines = [f"找到以下联系人（匹配“{keyword}”）："]
        else:
            lines = ["通讯录里目前这些联系人可用："]
        for item in contacts:
            name = str(item.get("display_name") or "未命名联系人").strip()
            open_id = str(item.get("open_id") or "").strip()
            matched = item.get("matched") if isinstance(item.get("matched"), dict) else {}
            parts = [name]
            if open_id:
                parts.append(f"open_id: {open_id}")
            email = str(matched.get("邮箱") or matched.get("email") or "").strip()
            if email:
                parts.append(f"邮箱: {email}")
            lines.append(f"- {'；'.join(parts)}")
        if has_more:
            lines.append("\n回复“继续”可查看剩余联系人。")
        return "\n".join(lines)

    async def handle(self, *, msg: InboundMessage, session: Session) -> OutboundMessage | None:
        intent = self._directory_intent(msg)
        if intent is None:
            return None
        if not self._loop.tools.has("bitable_directory_search"):
            return None

        keyword, limit = intent
        args: dict[str, Any] = {"limit": limit}
        if keyword:
            args["keyword"] = keyword
        result = await self._loop.tools.execute("bitable_directory_search", args)
        payload = extract_json_object(result)
        if not payload:
            return None

        error = str(payload.get("error") or "").strip()
        if error:
            content = f"通讯录查询失败：{error}"
        else:
            contacts = [item for item in payload.get("contacts", []) if isinstance(item, dict)]
            self._store_recent_hits(session, keyword=keyword, contacts=contacts)
            visible = contacts[: self._PAGE_SIZE]
            content = self._format_contacts(keyword, visible, has_more=len(contacts) > len(visible))

        self._record_direct_turn(session, msg, content)
        self._loop.sessions.save(session)
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=content,
            metadata={**(msg.metadata or {}), "_tool_turn": True},
        )
