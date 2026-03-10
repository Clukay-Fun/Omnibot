"""描述:
主要功能:
    - 提供向外部频道发送消息的工具能力。
"""

from typing import TYPE_CHECKING, Any, Awaitable, Callable

from nanobot.agent.tools.base import Tool
from nanobot.bus.events import OutboundMessage

if TYPE_CHECKING:
    from nanobot.agent.turn_runtime import TurnRuntime

#region 消息发送工具

class MessageTool(Tool):
    """
    用处: 主动发送消息的工具。

    功能:
        - 封装底层 OutboundMessage 结构，供 LLM 在合适时机触发外发消息并记录轮次状态。
    """

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
    ):
        """
        用处: 构造函数。参数 default_channel/chat_id: 默认消息目标。

        功能:
            - 初始化默认的发送上下文并挂载外部注入的回调闭包。
        """
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._default_message_id = default_message_id
        self._sent_in_turn: bool = False

    def set_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """
        用处: 动态设定消息分发上下文。

        功能:
            - 允许外部 AgentLoop 或 Coordinators 在每条消息收到时重置当前发信目标。
        """
        self._default_channel = channel
        self._default_chat_id = chat_id
        self._default_message_id = message_id

    def set_turn_runtime(self, runtime: "TurnRuntime") -> None:
        message_id = str(runtime.metadata.get("message_id") or "").strip() or None
        self.set_context(runtime.channel, runtime.chat_id, message_id)

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """
        用处: 挂载框架外发能力。参数 callback: 系统发送端点。

        功能:
            - 将框架级、平台无关的发送实现提供给工具类使用。
        """
        self._send_callback = callback

    def start_turn(self) -> None:
        """
        用处: 标记轮次开始边界。

        功能:
            - 重置 _sent_in_turn 为 False 以便检测单个 Agent 轮次内是否已经发送了消息。
        """
        self._sent_in_turn = False

    @property
    def name(self) -> str:
        """
        用处: 返回工具注册名。

        功能:
            - 名字为 message，这是模型唤起的唯一标识。
        """
        return "message"

    @property
    def description(self) -> str:
        """
        用处: 向 LLM 提供说明的摘要。

        功能:
            - 提供清晰的“向用户发消息”意图。
        """
        return "向用户发送一条消息。当你需要交流某些内容时使用它。"

    @property
    def parameters(self) -> dict[str, Any]:
        """
        用处: 入参约束。

        功能:
            - 要求至少传递内容 content，同时支持重定向投递频道以及挂载媒体文件列表。
        """
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要发送的消息内容"
                },
                "channel": {
                    "type": "string",
                    "description": "可选：目标频道（telegram、discord 等）"
                },
                "chat_id": {
                    "type": "string",
                    "description": "可选：目标聊天室/用户 ID"
                },
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选：需要附加的文件路径列表（图片、音频、文档）"
                }
            },
            "required": ["content"]
        }

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        media: list[str] | None = None,
        **kwargs: Any
    ) -> str:
        """
        用处: 执行真正的发消息步骤。

        功能:
            - 数据校验后拼装 OutboundMessage 模型交给回调试图广播并标记状态为成功。
        """
        channel = channel or self._default_channel
        chat_id = chat_id or self._default_chat_id
        message_id = message_id or self._default_message_id

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        if not self._send_callback:
            return "Error: Message sending not configured"

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=media or [],
            metadata={
                "message_id": message_id,
            },
        )

        try:
            await self._send_callback(msg)
            if channel == self._default_channel and chat_id == self._default_chat_id:
                self._sent_in_turn = True
            media_info = f" with {len(media)} attachments" if media else ""
            return f"Message sent to {channel}:{chat_id}{media_info}"
        except Exception as e:
            return f"Error sending message: {str(e)}"

#endregion
