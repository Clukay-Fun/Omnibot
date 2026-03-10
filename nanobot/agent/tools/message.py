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
    """用处，参数

    功能:
        - 封装消息发送流程并管理轮次发送状态。
    """

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
    ):
        """用处，参数

        功能:
            - 初始化默认发送上下文与回调。
        """
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._default_message_id = default_message_id
        self._sent_in_turn: bool = False

    def set_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """用处，参数

        功能:
            - 更新当前轮次默认发送目标。
        """
        self._default_channel = channel
        self._default_chat_id = chat_id
        self._default_message_id = message_id

    def set_turn_runtime(self, runtime: "TurnRuntime") -> None:
        message_id = str(runtime.metadata.get("message_id") or "").strip() or None
        self.set_context(runtime.channel, runtime.chat_id, message_id)

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """用处，参数

        功能:
            - 注入实际的消息发送实现。
        """
        self._send_callback = callback

    def start_turn(self) -> None:
        """用处，参数

        功能:
            - 在新轮次开始时重置发送标记。
        """
        self._sent_in_turn = False

    @property
    def name(self) -> str:
        """用处，参数

        功能:
            - 返回工具注册名称。
        """
        return "message"

    @property
    def description(self) -> str:
        """用处，参数

        功能:
            - 返回工具功能说明。
        """
        return "向用户发送一条消息。当你需要交流某些内容时使用它。"

    @property
    def parameters(self) -> dict[str, Any]:
        """用处，参数

        功能:
            - 定义工具入参 schema。
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
        """用处，参数

        功能:
            - 组装外发消息并通过回调发送。
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
