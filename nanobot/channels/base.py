"""聊天平台的频道基类接口。"""

from abc import ABC, abstractmethod
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus


# region [频道基类]

class BaseChannel(ABC):
    """
    聊天频道实现的抽象基类。
    
    每一个频道（Telegram、Discord 等）都应实现此接口，以便与 nanobot 消息总线集成。
    """
    
    name: str = "base"
    
    def __init__(self, config: Any, bus: MessageBus):
        """
        初始化频道。
        
        参数:
            config: 特定频道的配置。
            bus: 用于通信的消息总线。
        """
        self.config = config
        self.bus = bus
        self._running = False
    
    @abstractmethod
    async def start(self) -> None:
        """
        启动频道并开始监听消息。
        
        这应当是一个长时间运行的异步任务，它会：
        1. 连接到聊天平台
        2. 监听传入消息
        3. 通过 _handle_message() 将消息转发至总线
        """
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """停止频道并清理资源。"""
        pass
    
    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """
        通过此频道发送消息。
        
        参数:
            msg: 要发送的消息。
        """
        pass
    
    def is_allowed(self, sender_id: str) -> bool:
        """
        检查发送者是否具有使用该机器人的权限。
        
        参数:
            sender_id: 发送者的标识符。
        
        返回:
            如果拥有权限则返回 True，否则返回 False。
        """
        allow_list = getattr(self.config, "allow_from", [])
        
        # 如果没有配置 allow_list，默认允许所有人
        if not allow_list:
            return True
        
        sender_str = str(sender_id)
        if sender_str in allow_list:
            return True
        if "|" in sender_str:
            for part in sender_str.split("|"):
                if part and part in allow_list:
                    return True
        return False
    
    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
    ) -> None:
        """
        处理来自聊天平台的传入消息。
        
        该方法会检查权限并将消息转发给总线。
        
        参数:
            sender_id: 发送者的标识符。
            chat_id: 聊天/频道的标识符。
            content: 消息纯文本内容。
            media: 可选的媒体资源 URL 列表。
            metadata: 可选的频道特定元数据。
            session_key: 可选的会话 Key 覆盖（例如用于基于帖子的会话）。
        """
        if not self.is_allowed(sender_id):
            logger.warning(
                "Access denied for sender {} on channel {}. "
                "Add them to allowFrom list in config to grant access.",
                sender_id, self.name,
            )
            return
        
        msg = InboundMessage(
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            media=media or [],
            metadata=metadata or {},
            session_key_override=session_key,
        )
        
        await self.bus.publish_inbound(msg)
    
    @property
    def is_running(self) -> bool:
        """检查频道是否正在运行。"""
        return self._running

# endregion
