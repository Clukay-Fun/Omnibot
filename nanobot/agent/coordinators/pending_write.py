"""
描述: 处理结构化写入确认流程的协调器。
主要功能:
    - 拦截涉及重要数据变更的工具调用（如飞书多维表格的Upsert或删除操作）。
    - 暂存原始调用参数与Diff预览结构，直到用户在普通会话中回复“确认”或“取消”后才予以放行或丢弃。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.coordinators.base import AgentCoordinator, CoordinatorToolResult
from nanobot.agent.pending_write import (
    PENDING_WRITE_METADATA_KEY,
    coerce_pending_write_result,
    extract_json_object,
    extract_pending_write_command,
    format_pending_write_preview,
)
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.session.manager import Session

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop

#region 待写入确认协调器

class PendingWriteCoordinator(AgentCoordinator):
    """
    用处: 核心的待写入确认协调器类。

    功能:
        - 负责管理暂存高危写入指令（包含工具名、参数和确认 Token），并处理对应的二次确认回复逻辑。
    """
    def __init__(self, agent: AgentLoop) -> None:
        super().__init__(agent)

    @property
    def _loop(self) -> AgentLoop:
        assert self._agent is not None
        return self._agent

    @staticmethod
    def _pending_write(session: Session) -> dict[str, Any]:
        """
        用处: 获取当前会话中暂存的待写入状态元数据。

        功能:
            - 安全提取 `PENDING_WRITE_METADATA_KEY` 下的字典值。
        """
        value = session.metadata.get(PENDING_WRITE_METADATA_KEY)
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _set_pending_write(session: Session, payload: dict[str, Any]) -> None:
        """
        用处: 设置待写入状态至当前会话的元数据中。
        
        功能:
            - 持久化挂起的工具参数、Token和预览信息字典 payload。
        """
        metadata = dict(session.metadata or {})
        metadata[PENDING_WRITE_METADATA_KEY] = payload
        session.metadata = metadata

    @staticmethod
    def _clear_pending_write(session: Session) -> None:
        """
        用处: 清理当前会话的待写入状态。
        
        功能:
            - 当操作完成、过期或被取消时，将其从元数据中弹出丢弃。
        """
        metadata = dict(session.metadata or {})
        metadata.pop(PENDING_WRITE_METADATA_KEY, None)
        session.metadata = metadata

    @staticmethod
    def _record_direct_turn(session: Session, msg: InboundMessage, assistant_content: str) -> None:
        """
        用处: 将交互记录直接计入会话历史。

        功能:
            - 在执行取消/确认反馈后，将指令和操作结果文字双向固化为对话记录，为后续多轮提供参考。
        """
        session.add_message("user", msg.content)
        session.add_message("assistant", assistant_content)

    @staticmethod
    def _pending_write_args_from_payload(
        *,
        tool_name: str,
        raw_args: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """
        用处: 根据真实的 Dry Run 返回载荷更新并抽取最终待执行的工具参数字典。

        功能:
            - 补充多维表格的 `table_id` 或 `record_id`、写入字段等缺失但对正式写入至关重要的信息，同时剔除老旧 `confirm_token` 防止冲突。
        """
        args = dict(raw_args)
        args.pop("confirm_token", None)
        preview: dict[str, Any] = payload.get("preview") if isinstance(payload.get("preview"), dict) else {}
        if isinstance(preview.get("fields"), dict):
            args["fields"] = dict(preview["fields"])
        for key in ("table_id", "record_id"):
            value = preview.get(key)
            if value not in (None, ""):
                args[key] = value
        if tool_name == "bitable_delete" and "record_id" in raw_args:
            args.setdefault("record_id", raw_args.get("record_id"))
        return args

    def on_tool_result(
        self,
        *,
        session: Session,
        tool_name: str,
        raw_args: dict[str, Any],
        result: str,
    ) -> CoordinatorToolResult | None:
        """
        用处: 工具结果拦截处理方法。

        功能:
            - 解析工具首次执行后的返回（包含 `dry_run: true`）。
            - 生成挂起记录和确认令牌，阻断立刻写入的通道，并将预览（Diff）信息抛出给用户渲染卡片。
        """
        payload = extract_json_object(result)
        if not payload or payload.get("dry_run") is not True or not payload.get("confirm_token"):
            return None
        preview: dict[str, Any] = payload.get("preview") if isinstance(payload.get("preview"), dict) else {}
        if preview.get("table_id"):
            metadata = dict(session.metadata or {})
            previous: dict[str, Any] = (
                metadata.get("recent_selected_table") if isinstance(metadata.get("recent_selected_table"), dict) else {}
            )
            metadata["recent_selected_table"] = {
                **dict(previous),
                "table_id": str(preview.get("table_id") or ""),
                "table_name": str(previous.get("table_name") or previous.get("name") or "").strip(),
            }
            session.metadata = metadata
        pending = {
            "tool": tool_name,
            "token": str(payload.get("confirm_token") or ""),
            "args": self._pending_write_args_from_payload(tool_name=tool_name, raw_args=raw_args, payload=payload),
            "preview": preview,
            "created_at": self._loop._now_iso(),
        }
        self._set_pending_write(session, pending)
        return CoordinatorToolResult(final_content=format_pending_write_preview(preview))

    async def handle(self, *, msg: InboundMessage, session: Session) -> OutboundMessage | None:
        """
        用处: 用户消息命令处理入口。

        功能:
            - 若存在挂起的写入请求，尝试使用精确正则或自然语义匹配 `msg` 中是否包含“确定”、“取消”。
            - 匹配成功后，执行真实的 `tool.execute()`，或清理状态直接返回已取消。
        """
        pending = self._pending_write(session)
        if not pending:
            return None

        command, token, extra_text = extract_pending_write_command(msg)
        pending_token = str(pending.get("token") or "")
        if token and token != pending_token:
            content = "当前没有匹配的待确认写入。请按最新预览直接回复“确认”或“取消”。"
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata={**(msg.metadata or {}), "_tool_turn": True},
            )

        if command == "cancel":
            self._clear_pending_write(session)
            content = "已取消这次待写入操作。"
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata={**(msg.metadata or {}), "_tool_turn": True},
            )

        if command == "confirm" and not extra_text:
            args = dict(pending.get("args") or {})
            tool_name = str(pending.get("tool") or "")
            args["confirm_token"] = pending_token
            result = await self._loop.tools.execute(tool_name, args)
            self._clear_pending_write(session)
            payload = extract_json_object(result)
            content = coerce_pending_write_result(payload) if payload else result
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata={**(msg.metadata or {}), "_tool_turn": True},
            )

        return None

#endregion
