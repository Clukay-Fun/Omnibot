"""描述:
主要功能:
    - 提供基于 botpy 的 QQ 频道收发实现。
"""

import asyncio
from collections import deque
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import QQConfig

try:
    import botpy
    from botpy.message import C2CMessage

    QQ_AVAILABLE = True
except ImportError:
    QQ_AVAILABLE = False
    botpy = None
    C2CMessage = None

if TYPE_CHECKING:
    from botpy.message import C2CMessage


#region 辅助方法

def _make_bot_class(channel: "QQChannel") -> "type[botpy.Client]":
    """用处，参数

    功能:
        - 构造绑定当前频道实例的 botpy 客户端子类。
    """
    intents = botpy.Intents(public_messages=True, direct_message=True)

    class _Bot(botpy.Client):
        def __init__(self):
            # 禁用 botpy 原生的文件日志机制 — 因为 nanobot 使用 loguru; 并且默认使用 "botpy.log" 有时会在只读文件系统中遇到阻断错误
            super().__init__(intents=intents, ext_handlers=False)

        async def on_ready(self):
            logger.info("QQ bot ready: {}", self.robot.name)

        async def on_c2c_message_create(self, message: "C2CMessage"):
            await channel._on_message(message)

        async def on_direct_message_create(self, message):
            await channel._on_message(message)

    return _Bot

#endregion

#region QQ频道核心类

class QQChannel(BaseChannel):
    """用处，参数

    功能:
        - 管理 QQ 频道连接并处理消息收发。
    """

    name = "qq"

    def __init__(self, config: QQConfig, bus: MessageBus):
        """用处，参数

        功能:
            - 初始化配置、客户端句柄和去重缓存。
        """
        super().__init__(config, bus)
        self.config: QQConfig = config
        self._client: "botpy.Client | None" = None
        self._processed_ids: deque = deque(maxlen=1000)

    async def start(self) -> None:
        """用处，参数

        功能:
            - 启动 QQ 客户端并进入运行循环。
        """
        if not QQ_AVAILABLE:
            logger.error("QQ SDK not installed. Run: pip install qq-botpy")
            return

        if not self.config.app_id or not self.config.secret:
            logger.error("QQ app_id and secret not configured")
            return

        self._running = True
        BotClass = _make_bot_class(self)
        self._client = BotClass()

        logger.info("QQ bot started (C2C private message)")
        await self._run_bot()

    async def _run_bot(self) -> None:
        """用处，参数

        功能:
            - 运行客户端并在异常后自动重连。
        """
        while self._running:
            try:
                await self._client.start(appid=self.config.app_id, secret=self.config.secret)
            except Exception as e:
                logger.warning("QQ bot error: {}", e)
            if self._running:
                logger.info("Reconnecting QQ bot in 5 seconds...")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """用处，参数

        功能:
            - 停止运行并关闭客户端连接。
        """
        self._running = False
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
        logger.info("QQ bot stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """用处，参数

        功能:
            - 通过 QQ API 发送外发消息。
        """
        if not self._client:
            logger.warning("QQ client not initialized")
            return
        try:
            msg_id = msg.metadata.get("message_id")
            await self._client.api.post_c2c_message(
                openid=msg.chat_id,
                msg_type=0,
                content=msg.content,
                msg_id=msg_id,
            )
        except Exception as e:
            logger.error("Error sending QQ message: {}", e)

    async def _on_message(self, data: "C2CMessage") -> None:
        """用处，参数

        功能:
            - 解析入站消息并转发到统一总线。
        """
        try:
            # 根据其附带的 message ID 执行基本重复项合并策略处理
            if data.id in self._processed_ids:
                return
            self._processed_ids.append(data.id)

            author = data.author
            user_id = str(getattr(author, 'id', None) or getattr(author, 'user_openid', 'unknown'))
            content = (data.content or "").strip()
            if not content:
                return

            await self._handle_message(
                sender_id=user_id,
                chat_id=user_id,
                content=content,
                metadata={"message_id": data.id},
            )
        except Exception:
            logger.exception("Error handling QQ message")

#endregion
