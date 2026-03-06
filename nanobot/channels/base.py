"""描述:
主要功能:
    - 定义频道实现的统一抽象接口。
"""

from abc import ABC, abstractmethod
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus


#region 频道基类

class BaseChannel(ABC):
    """用处，参数

    功能:
        - 约束各频道的启动、停止、发送与入站处理行为。
    """
    
    name: str = "base"
    
    def __init__(self, config: Any, bus: MessageBus):
        """用处，参数

        功能:
            - 保存配置与消息总线并初始化运行状态。
        """
        self.config = config
        self.bus = bus
        self._running = False
    
    @abstractmethod
    async def start(self) -> None:
        """用处，参数

        功能:
            - 启动频道连接并进入消息监听循环。
        """
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """用处，参数

        功能:
            - 停止频道并释放连接资源。
        """
        pass
    
    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """用处，参数

        功能:
            - 将外发消息投递到具体平台。
        """
        pass
    
    def is_allowed(self, sender_id: str) -> bool:
        """用处，参数

        功能:
            - 根据 allow_list 规则判断发送者是否可用。
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
        """用处，参数

        功能:
            - 校验权限并把入站消息写入总线。
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
        """用处，参数

        功能:
            - 返回频道当前运行状态。
        """
        return self._running

#endregion
