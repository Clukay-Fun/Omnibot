"""描述:
主要功能:
    - 定义消息总线的入站与出站事件结构。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


#region 消息事件

@dataclass
class InboundMessage:
    """用处，参数

    功能:
        - 表示来自频道侧的入站消息。
    """
    
    channel: str  # telegram, discord, slack, whatsapp
    sender_id: str  # User identifier
    chat_id: str  # Chat/channel identifier
    content: str  # Message text
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data
    session_key_override: str | None = None  # Optional override for thread-scoped sessions
    
    @property
    def session_key(self) -> str:
        """用处，参数

        功能:
            - 生成会话唯一键并优先使用覆盖值。
        """
        return self.session_key_override or f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    """用处，参数

    功能:
        - 表示需要发送到频道侧的外发消息。
    """
    
    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


#endregion
