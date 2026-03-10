"""
描述: 处理上下文本延续和分页指令的协调器。
主要功能:
    - 拦截诸如“继续”、“下一页”等用户确定性指令。
    - 负责维持并展示联系人列表、候选记录等多行较长数据的分页结果回放。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.coordinators.base import AgentCoordinator
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.session.manager import Session

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop

#region 上下文延续协调器

class ContinuationCoordinator(AgentCoordinator):
    """
    用处: 上下文延续协调器类。继承自 AgentCoordinator。

    功能:
        - 拦截延续命令，读取当前会话状态中的分页偏移量，并组装下一页内容返回给用户。
    """
    _COMMANDS = {"继续", "更多", "下一页", "more", "continue", "next"}

    def __init__(self, agent: "AgentLoop") -> None:
        super().__init__(agent)

    @property
    def _loop(self) -> "AgentLoop":
        assert self._agent is not None
        return self._agent

    @staticmethod
    def _record_direct_turn(session: Session, msg: InboundMessage, assistant_content: str) -> None:
        """
        用处: 直接在会话中记录一对交互轮次。参数为 session, 用户消息 msg, 助手回复文本 assistant_content。

        功能:
            - 将当前用户的指令和协调器生成的回复直接写入历史，跳过大语言模型的推演。
        """
        session.add_message("user", msg.content)
        session.add_message("assistant", assistant_content)

    @classmethod
    def _is_continuation(cls, text: str) -> bool:
        """
        用处: 判断文本是否为延续指令。参数为用户输入的文本 text。

        功能:
            - 通过精准匹配内部预设命令集合，拦截指令。
        """
        return text.strip().lower() in cls._COMMANDS

    @staticmethod
    def _format_directory_contacts(contacts: list[dict[str, Any]], *, remaining: int = 0) -> str:
        """
        用处: 格式化人员目录联系人以便展示。参数为联系人字典列表 contacts 以及剩余条数 remaining。

        功能:
            - 将底层返回的飞书联系人数组转化为具有高可读性的自然语言文本描述。
        """
        if not contacts:
            return "没有更多联系人可展示了。"
        lines = ["继续展示联系人："]
        for item in contacts:
            name = str(item.get("display_name") or item.get("open_id") or "未命名联系人").strip()
            open_id = str(item.get("open_id") or "").strip()
            parts = [name]
            if open_id:
                parts.append(f"open_id: {open_id}")
            lines.append(f"- {'；'.join(parts)}")
        if remaining > 0:
            lines.append(f"\n还有 {remaining} 条，回复“继续”查看。")
        return "\n".join(lines)

    @staticmethod
    def _format_selection_options(
        selection: dict[str, Any],
        items: list[dict[str, Any]],
        *,
        remaining: int = 0,
        start_index: int = 1,
    ) -> str:
        """
        用处: 格式化选项目录/结果表中的候选项，用于分页展示。

        功能:
            - 根据不同的类型 (table_candidates / record_candidates) 前缀生成结构化的项列表信息和索引号。
        """
        kind = str(selection.get("kind") or "options")
        title = "继续展示候选项：" if kind == "table_candidates" else "继续展示可选项："
        if kind == "record_candidates":
            title = "继续展示候选记录："
        lines = [title]
        for idx, item in enumerate(items, start=start_index):
            label = str(
                item.get("name")
                or item.get("display_name")
                or item.get("table_id")
                or item.get("open_id")
                or item.get("record_id")
                or "未命名项"
            )
            lines.append(f"- {idx}. {label}")
        if remaining > 0:
            lines.append(f"\n还有 {remaining} 条，回复“继续”查看。")
        return "\n".join(lines)

    async def handle(self, *, msg: InboundMessage, session: Session) -> OutboundMessage | None:
        """
        用处: 处理入口函数。参数为输入消息 msg 以及上下文会话 session。

        功能:
            - 一旦匹配为 continuation 命令，则立即接管回复流程，阻断大语言模型请求。
            - 提取元数据中的未展示尽的分页结果，计算最新的区间范围后触发重新展示。
        """
        if not self._is_continuation(msg.content):
            return None

        metadata = dict(session.metadata or {})
        selection = metadata.get("result_selection") if isinstance(metadata.get("result_selection"), dict) else {}
        offset = int(selection.get("offset") or 0) if selection else 0
        page_size = max(1, int(selection.get("page_size") or 5)) if selection else 5
        had_local_continuation_state = bool(selection) or bool(metadata.get("recent_directory_hits"))

        if selection and isinstance(selection.get("items"), list):
            items = [dict(item) for item in selection.get("items", []) if isinstance(item, dict)]
            if offset < len(items):
                next_items = items[offset : offset + page_size]
                start_index = offset + 1
                selection["offset"] = min(len(items), offset + page_size)
                metadata["result_selection"] = selection
                session.metadata = metadata
                remaining = max(0, len(items) - int(selection["offset"]))
                content = self._format_selection_options(
                    selection,
                    next_items,
                    remaining=remaining,
                    start_index=start_index,
                )
                self._record_direct_turn(session, msg, content)
                self._loop.sessions.save(session)
                return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})

        contacts = metadata.get("recent_directory_hits") if isinstance(metadata.get("recent_directory_hits"), list) else []
        offset = int(metadata.get("recent_directory_offset") or 0)
        if contacts and offset < len(contacts):
            next_contacts = [dict(item) for item in contacts[offset : offset + page_size] if isinstance(item, dict)]
            offset = min(len(contacts), offset + page_size)
            metadata["recent_directory_offset"] = offset
            session.metadata = metadata
            remaining = max(0, len(contacts) - offset)
            content = self._format_directory_contacts(next_contacts, remaining=remaining)
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})

        if not had_local_continuation_state:
            return None

        content = self._loop._runtime_text.prompt_text("pagination", "no_more_content", "没有可继续的内容了。")
        self._record_direct_turn(session, msg, content)
        self._loop.sessions.save(session)
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})

#endregion
