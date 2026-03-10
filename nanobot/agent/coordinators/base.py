"""
描述: 基础协调器接口，用于在确定的LLM多轮对话中处理拦截和协调。
主要功能:
    - 定义了所有 Agent 协调器的基础类 `AgentCoordinator` 和回传结果结构 `CoordinatorToolResult`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.session.manager import Session

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop

#region 数据结构与基础协调器


@dataclass(slots=True)
class CoordinatorToolResult:
    final_content: str


class AgentCoordinator:
    """
    用处: 基础的 Agent 协调器类，作为其他具体协调器的基类。参数 agent 为绑定的 AgentLoop 实例。

    功能:
        - 提供协调器基础的名称、处理入口和工具结果拦截入口。
    """
    def __init__(self, agent: AgentLoop | None = None) -> None:
        self._agent = agent

    @property
    def name(self) -> str:
        return self.__class__.__name__

    async def handle(self, *, msg: InboundMessage, session: Session) -> OutboundMessage | None:
        _ = (msg, session)
        return None

    def on_tool_result(
        self,
        *,
        session: Session,
        tool_name: str,
        raw_args: dict[str, Any],
        result: str,
    ) -> CoordinatorToolResult | None:
        """
        用处: 工具执行结果回调拦截器。参数为会话状态、工具名、参数和原始结果。

        功能:
            - 允许协调器在工具执行完毕后，介入并修改最终返回给用户的内容。
        """
        _ = (session, tool_name, raw_args, result)
        return None

#endregion
