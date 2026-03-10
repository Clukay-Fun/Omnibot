"""描述:
主要功能:
    - 提供解耦频道与智能体的异步消息总线。
"""

import asyncio

from nanobot.bus.events import InboundMessage, OutboundMessage

#region 消息总线

class MessageBus:
    """用处，参数

    功能:
        - 管理入站和出站队列的发布与消费。
    """

    def __init__(self):
        """用处，参数

        功能:
            - 初始化空的入站与出站队列。
        """
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """用处，参数

        功能:
            - 将频道消息写入入站队列。
        """
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """用处，参数

        功能:
            - 读取下一条入站消息。
        """
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """用处，参数

        功能:
            - 将智能体响应写入出站队列。
        """
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """用处，参数

        功能:
            - 读取下一条出站消息。
        """
        return await self.outbound.get()

    @property
    def inbound_size(self) -> int:
        """用处，参数

        功能:
            - 返回当前入站队列长度。
        """
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """用处，参数

        功能:
            - 返回当前出站队列长度。
        """
        return self.outbound.qsize()


#endregion
