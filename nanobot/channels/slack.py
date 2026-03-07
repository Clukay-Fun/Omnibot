"""描述:
主要功能:
    - 提供基于 Socket Mode 的 Slack 频道收发实现。
"""

import asyncio
import re

from loguru import logger
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.socket_mode.websockets import SocketModeClient
from slack_sdk.web.async_client import AsyncWebClient
from slackify_markdown import slackify_markdown

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import SlackConfig

#region Slack频道核心类

class SlackChannel(BaseChannel):
    """用处，参数

    功能:
        - 处理 Slack 事件接入、过滤与消息发送。
    """

    name = "slack"

    def __init__(self, config: SlackConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: SlackConfig = config
        self._web_client: AsyncWebClient | None = None
        self._socket_client: SocketModeClient | None = None
        self._bot_user_id: str | None = None

    async def start(self) -> None:
        """启动针对 Slack Socket Mode 的工作客户端主体例程架构网络。"""
        if not self.config.bot_token or not self.config.app_token:
            logger.error("Slack bot/app token not configured")
            return
        if self.config.mode != "socket":
            logger.error("Unsupported Slack mode: {}", self.config.mode)
            return

        self._running = True

        self._web_client = AsyncWebClient(token=self.config.bot_token)
        self._socket_client = SocketModeClient(
            app_token=self.config.app_token,
            web_client=self._web_client,
        )

        self._socket_client.socket_mode_request_listeners.append(self._on_socket_request)

        # 解析得到机器人账号的实际 User ID，以此为之后的 At (@) 提及处理打下标识符基础
        try:
            auth = await self._web_client.auth_test()
            self._bot_user_id = auth.get("user_id")
            logger.info("Slack bot connected as {}", self._bot_user_id)
        except Exception as e:
            logger.warning("Slack auth_test failed: {}", e)

        logger.info("Starting Slack Socket Mode client...")
        await self._socket_client.connect()

        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """针对该频道实例停止工作机制中的客户端。"""
        self._running = False
        if self._socket_client:
            try:
                await self._socket_client.close()
            except Exception as e:
                logger.warning("Slack socket close failed: {}", e)
            self._socket_client = None

    async def send(self, msg: OutboundMessage) -> None:
        """通过 Slack 环境发布对外生成的应答讯息数据。"""
        if not self._web_client:
            logger.warning("Slack client not running")
            return
        try:
            slack_meta = msg.metadata.get("slack", {}) if msg.metadata else {}
            thread_ts = slack_meta.get("thread_ts")
            channel_type = slack_meta.get("channel_type")
            # 只有对频道/群组当中的对话内容才会尝试以 Thread (短信序列) 形式作答；传统的 DM（私信）交流一般不会使用线程序列形态
            use_thread = thread_ts and channel_type != "im"
            thread_ts_param = thread_ts if use_thread else None

            if msg.content:
                await self._web_client.chat_postMessage(
                    channel=msg.chat_id,
                    text=self._to_mrkdwn(msg.content),
                    thread_ts=thread_ts_param,
                )

            for media_path in msg.media or []:
                try:
                    await self._web_client.files_upload_v2(
                        channel=msg.chat_id,
                        file=media_path,
                        thread_ts=thread_ts_param,
                    )
                except Exception as e:
                    logger.error("Failed to upload file {}: {}", media_path, e)
        except Exception as e:
            logger.error("Error sending Slack message: {}", e)

    async def _on_socket_request(
        self,
        client: SocketModeClient,
        req: SocketModeRequest,
    ) -> None:
        """作为回调目标接管处理经 Socket Mode 转送到达的一切网络 Request 请求包数据。"""
        if req.type != "events_api":
            return

        # 第一时间送出回执 Acknowledge 确认操作收到
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )

        payload = req.payload or {}
        event = payload.get("event") or {}
        event_type = event.get("type")

        # 进行分支甄选只去响应系统层面有关的被提起提及 ("app_mention") 或是基础发言 ("message") 事件动作
        if event_type not in ("message", "app_mention"):
            return

        sender_id = event.get("user")
        chat_id = event.get("channel")

        # 丢弃一切不来自于真人的机器播报类/系统的副类别发言产出活动 (任意包含 subtype 非主分类标记的行为=皆等价视为非正常标准真人产生的发言)
        if event.get("subtype"):
            return
        if self._bot_user_id and sender_id == self._bot_user_id:
            return

        # 为避免双重执行产生重复: 当有 At (@) 动作发生在公开交流的开放通道中时 Slack 的通知事件经常既会有 `message` 又附上了 `app_mention`
        # 总体原则通常是以 `app_mention` 作为我们被响应和驱动业务的前提重点选项去处理。
        text = event.get("text") or ""
        if event_type == "message" and self._bot_user_id and f"<@{self._bot_user_id}>" in text:
            return

        # Debug 阶段: 打印一下这部分网络通知活动载荷的大致全貌状态去分析
        logger.debug(
            "Slack event: type={} subtype={} user={} channel={} channel_type={} text={}",
            event_type,
            event.get("subtype"),
            sender_id,
            chat_id,
            event.get("channel_type"),
            text[:80],
        )
        if not sender_id or not chat_id:
            return

        channel_type = event.get("channel_type") or ""

        if not self._is_allowed(sender_id, chat_id, channel_type):
            return

        if channel_type != "im" and not self._should_respond_in_channel(event_type, text, chat_id):
            return

        text = self._strip_bot_mention(text)

        thread_ts = event.get("thread_ts")
        if self.config.reply_in_thread and not thread_ts:
            thread_ts = event.get("ts")
        # 追加添加 :eyes: 这个代表已经被我们接收并关注注视过的 Emoji 表演回应动作到发出引发执行条件的那个起始话题消息载体（只是为了做到尽可能的表现回显服务反馈响应而已）
        try:
            if self._web_client and event.get("ts"):
                await self._web_client.reactions_add(
                    channel=chat_id,
                    name=self.config.react_emoji,
                    timestamp=event.get("ts"),
                )
        except Exception as e:
            logger.debug("Slack reactions_add failed: {}", e)

        # 使用基于短线索(Thread)上下文划分机制作为会话 Session key 在群聊和群通信频道去识别用户，否则采用默认通用方式
        session_key = f"slack:{chat_id}:{thread_ts}" if thread_ts and channel_type != "im" else None

        try:
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=text,
                metadata={
                    "slack": {
                        "event": event,
                        "thread_ts": thread_ts,
                        "channel_type": channel_type,
                    },
                },
                session_key=session_key,
            )
        except Exception:
            logger.exception("Error handling Slack message from {}", sender_id)

    def _is_allowed(self, sender_id: str, chat_id: str, channel_type: str) -> bool:
        """用处，参数

        功能:
            - 按 DM/群组策略判断是否允许处理消息。
        """
        if channel_type == "im":
            if not self.config.dm.enabled:
                return False
            if self.config.dm.policy == "allowlist":
                return sender_id in self.config.dm.allow_from
            return True

        # Group / channel messages
        if self.config.group_policy == "allowlist":
            return chat_id in self.config.group_allow_from
        return True

    def _should_respond_in_channel(self, event_type: str, text: str, chat_id: str) -> bool:
        """用处，参数

        功能:
            - 根据群组策略判断是否应在频道中回应。
        """
        if self.config.group_policy == "open":
            return True
        if self.config.group_policy == "mention":
            if event_type == "app_mention":
                return True
            return self._bot_user_id is not None and f"<@{self._bot_user_id}>" in text
        if self.config.group_policy == "allowlist":
            return chat_id in self.config.group_allow_from
        return False

    def _strip_bot_mention(self, text: str) -> str:
        """用处，参数

        功能:
            - 移除消息中对机器人的提及标记。
        """
        if not text or not self._bot_user_id:
            return text
        return re.sub(rf"<@{re.escape(self._bot_user_id)}>\s*", "", text).strip()

    _TABLE_RE = re.compile(r"(?m)^\|.*\|$(?:\n\|[\s:|-]*\|$)(?:\n\|.*\|$)*")
    _CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
    _INLINE_CODE_RE = re.compile(r"`[^`]+`")
    _LEFTOVER_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    _LEFTOVER_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
    _BARE_URL_RE = re.compile(r"(?<![|<])(https?://\S+)")

    @classmethod
    def _to_mrkdwn(cls, text: str) -> str:
        """把基础常规文本结构的 Markdown 排版要素给向包含表 Table 等各种额外细节的专属定置化 Slack mrkdwn 文本体制转换映射。"""
        if not text:
            return ""
        text = cls._TABLE_RE.sub(cls._convert_table, text)
        return cls._fixup_mrkdwn(slackify_markdown(text))

    @classmethod
    def _fixup_mrkdwn(cls, text: str) -> str:
        """在原有基础上补救处理好来自于 slackify_markdown 第三方接口库通常总会疏忽并遗忘遗留过滤的种种标记排版的显示弊病或乱写形式。"""
        code_blocks: list[str] = []

        def _save_code(m: re.Match) -> str:
            code_blocks.append(m.group(0))
            return f"\x00CB{len(code_blocks) - 1}\x00"

        text = cls._CODE_FENCE_RE.sub(_save_code, text)
        text = cls._INLINE_CODE_RE.sub(_save_code, text)
        text = cls._LEFTOVER_BOLD_RE.sub(r"*\1*", text)
        text = cls._LEFTOVER_HEADER_RE.sub(r"*\1*", text)
        text = cls._BARE_URL_RE.sub(lambda m: m.group(0).replace("&amp;", "&"), text)

        for i, block in enumerate(code_blocks):
            text = text.replace(f"\x00CB{i}\x00", block)
        return text

    @staticmethod
    def _convert_table(match: re.Match) -> str:
        """完成具体某一个独立的 Markdown 标准构建的数据列表向当前频道要求的阅读友好 Slack 可靠读取展现的序列转化过程。"""
        lines = [ln.strip() for ln in match.group(0).strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            return match.group(0)
        headers = [h.strip() for h in lines[0].strip("|").split("|")]
        start = 2 if re.fullmatch(r"[|\s:\-]+", lines[1]) else 1
        rows: list[str] = []
        for line in lines[start:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            cells = (cells + [""] * len(headers))[: len(headers)]
            parts = [f"**{headers[i]}**: {cells[i]}" for i in range(len(headers)) if cells[i]]
            if parts:
                rows.append(" · ".join(parts))
        return "\n".join(rows)

#endregion
