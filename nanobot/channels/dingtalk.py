"""描述:
主要功能:
    - 提供基于 Stream Mode 的钉钉频道收发实现。
"""

import asyncio
import json
import mimetypes
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import DingTalkConfig

try:
    from dingtalk_stream import (
        AckMessage,
        CallbackHandler,
        CallbackMessage,
        Credential,
        DingTalkStreamClient,
    )
    from dingtalk_stream.chatbot import ChatbotMessage

    DINGTALK_AVAILABLE = True
except ImportError:
    DINGTALK_AVAILABLE = False
    # Fallback so class definitions don't crash at module level
    CallbackHandler = object  # type: ignore[assignment,misc]
    CallbackMessage = None  # type: ignore[assignment,misc]
    AckMessage = None  # type: ignore[assignment,misc]
    ChatbotMessage = None  # type: ignore[assignment,misc]


#region DingTalk回调处理器

class NanobotDingTalkHandler(CallbackHandler):
    """用处，参数

    功能:
        - 解析 Stream 回调并转发到频道消息处理。
    """

    def __init__(self, channel: "DingTalkChannel"):
        super().__init__()
        self.channel = channel

    async def process(self, message: CallbackMessage):
        """处理传入的流消息。"""
        try:
            # 使用 SDK 提供的 ChatbotMessage 以进行更鲁棒的处理
            chatbot_msg = ChatbotMessage.from_dict(message.data)

            # 提取文本内容；如果 SDK 对象为空则回退到直接访问字典模式
            content = ""
            if chatbot_msg.text:
                content = chatbot_msg.text.content.strip()
            if not content:
                content = message.data.get("text", {}).get("content", "").strip()

            if not content:
                logger.warning(
                    "Received empty or unsupported message type: {}",
                    chatbot_msg.message_type,
                )
                return AckMessage.STATUS_OK, "OK"

            sender_id = chatbot_msg.sender_staff_id or chatbot_msg.sender_id
            sender_name = chatbot_msg.sender_nick or "Unknown"

            logger.info("Received DingTalk message from {} ({}): {}", sender_name, sender_id, content)

            # 以非阻塞的方式将其转发至 Nanobot 的 _on_message 处理
            # 存储任务引用以防在任务完成前被垃圾回收 (GC) 回收掉
            task = asyncio.create_task(
                self.channel._on_message(content, sender_id, sender_name)
            )
            self.channel._background_tasks.add(task)
            task.add_done_callback(self.channel._background_tasks.discard)

            return AckMessage.STATUS_OK, "OK"

        except Exception as e:
            logger.error("Error processing DingTalk message: {}", e)
            # 返回 OK 以避免 DingTalk 服务器不断重试
            return AckMessage.STATUS_OK, "Error"


#endregion

#region DingTalk频道核心类

class DingTalkChannel(BaseChannel):
    """用处，参数

    功能:
        - 管理钉钉事件接入、鉴权与消息发送流程。
    """

    name = "dingtalk"
    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
    _AUDIO_EXTS = {".amr", ".mp3", ".wav", ".ogg", ".m4a", ".aac"}
    _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

    def __init__(self, config: DingTalkConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: DingTalkConfig = config
        self._client: Any = None
        self._http: httpx.AsyncClient | None = None

        # 用于发送消息的 Access Token 管理
        self._access_token: str | None = None
        self._token_expiry: float = 0

        # 持有后台任务的引用以防垃圾回收
        self._background_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """在 Stream 模式下启动 DingTalk 机器人。"""
        try:
            if not DINGTALK_AVAILABLE:
                logger.error(
                    "DingTalk Stream SDK not installed. Run: pip install dingtalk-stream"
                )
                return

            if not self.config.client_id or not self.config.client_secret:
                logger.error("DingTalk client_id and client_secret not configured")
                return

            self._running = True
            self._http = httpx.AsyncClient()

            logger.info(
                "Initializing DingTalk Stream Client with Client ID: {}...",
                self.config.client_id,
            )
            credential = Credential(self.config.client_id, self.config.client_secret)
            self._client = DingTalkStreamClient(credential)

            # 注册标准的事件处理器
            handler = NanobotDingTalkHandler(self)
            self._client.register_callback_handler(ChatbotMessage.TOPIC, handler)

            logger.info("DingTalk bot started with Stream Mode")

            # 自动重连轮询机制：一旦 SDK 退出或崩溃则尝试重启会话流
            while self._running:
                try:
                    await self._client.start()
                except Exception as e:
                    logger.warning("DingTalk stream error: {}", e)
                if self._running:
                    logger.info("Reconnecting DingTalk stream in 5 seconds...")
                    await asyncio.sleep(5)

        except Exception as e:
            logger.exception("Failed to start DingTalk channel: {}", e)

    async def stop(self) -> None:
        """停止 DingTalk 机器人。"""
        self._running = False
        # 关闭共享的 HTTP 请求客户端
        if self._http:
            await self._http.aclose()
            self._http = None
        # 取消已在排队或执行中的后台任务
        for task in self._background_tasks:
            task.cancel()
        self._background_tasks.clear()

    async def _get_access_token(self) -> str | None:
        """获取或刷新 Access Token。"""
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        data = {
            "appKey": self.config.client_id,
            "appSecret": self.config.client_secret,
        }

        if not self._http:
            logger.warning("DingTalk HTTP client not initialized, cannot refresh token")
            return None

        try:
            resp = await self._http.post(url, json=data)
            resp.raise_for_status()
            res_data = resp.json()
            self._access_token = res_data.get("accessToken")
            # 提前 60 秒触发过期机制，以策安全
            self._token_expiry = time.time() + int(res_data.get("expireIn", 7200)) - 60
            return self._access_token
        except Exception as e:
            logger.error("Failed to get DingTalk access token: {}", e)
            return None

    @staticmethod
    def _is_http_url(value: str) -> bool:
        """用处，参数

        功能:
            - 判断字符串是否为 HTTP/HTTPS 地址。
        """
        return urlparse(value).scheme in ("http", "https")

    def _guess_upload_type(self, media_ref: str) -> str:
        """用处，参数

        功能:
            - 根据扩展名推断钉钉上传类型。
        """
        ext = Path(urlparse(media_ref).path).suffix.lower()
        if ext in self._IMAGE_EXTS:
            return "image"
        if ext in self._AUDIO_EXTS:
            return "voice"
        if ext in self._VIDEO_EXTS:
            return "video"
        return "file"

    def _guess_filename(self, media_ref: str, upload_type: str) -> str:
        """用处，参数

        功能:
            - 生成媒体上传时使用的文件名。
        """
        name = os.path.basename(urlparse(media_ref).path)
        return name or {"image": "image.jpg", "voice": "audio.amr", "video": "video.mp4"}.get(upload_type, "file.bin")

    async def _read_media_bytes(
        self,
        media_ref: str,
    ) -> tuple[bytes | None, str | None, str | None]:
        if not media_ref:
            return None, None, None

        if self._is_http_url(media_ref):
            if not self._http:
                return None, None, None
            try:
                resp = await self._http.get(media_ref, follow_redirects=True)
                if resp.status_code >= 400:
                    logger.warning(
                        "DingTalk media download failed status={} ref={}",
                        resp.status_code,
                        media_ref,
                    )
                    return None, None, None
                content_type = (resp.headers.get("content-type") or "").split(";")[0].strip()
                filename = self._guess_filename(media_ref, self._guess_upload_type(media_ref))
                return resp.content, filename, content_type or None
            except Exception as e:
                logger.error("DingTalk media download error ref={} err={}", media_ref, e)
                return None, None, None

        try:
            if media_ref.startswith("file://"):
                parsed = urlparse(media_ref)
                local_path = Path(unquote(parsed.path))
            else:
                local_path = Path(os.path.expanduser(media_ref))
            if not local_path.is_file():
                logger.warning("DingTalk media file not found: {}", local_path)
                return None, None, None
            data = await asyncio.to_thread(local_path.read_bytes)
            content_type = mimetypes.guess_type(local_path.name)[0]
            return data, local_path.name, content_type
        except Exception as e:
            logger.error("DingTalk media read error ref={} err={}", media_ref, e)
            return None, None, None

    async def _upload_media(
        self,
        token: str,
        data: bytes,
        media_type: str,
        filename: str,
        content_type: str | None,
    ) -> str | None:
        """用处，参数

        功能:
            - 上传媒体并返回可发送的 media_id。
        """
        if not self._http:
            return None
        url = f"https://oapi.dingtalk.com/media/upload?access_token={token}&type={media_type}"
        mime = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        files = {"media": (filename, data, mime)}

        try:
            resp = await self._http.post(url, files=files)
            text = resp.text
            result = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code >= 400:
                logger.error("DingTalk media upload failed status={} type={} body={}", resp.status_code, media_type, text[:500])
                return None
            errcode = result.get("errcode", 0)
            if errcode != 0:
                logger.error("DingTalk media upload api error type={} errcode={} body={}", media_type, errcode, text[:500])
                return None
            sub = result.get("result") or {}
            media_id = result.get("media_id") or result.get("mediaId") or sub.get("media_id") or sub.get("mediaId")
            if not media_id:
                logger.error("DingTalk media upload missing media_id body={}", text[:500])
                return None
            return str(media_id)
        except Exception as e:
            logger.error("DingTalk media upload error type={} err={}", media_type, e)
            return None

    async def _send_batch_message(
        self,
        token: str,
        chat_id: str,
        msg_key: str,
        msg_param: dict[str, Any],
    ) -> bool:
        """用处，参数

        功能:
            - 调用批量发送接口投递单条消息。
        """
        if not self._http:
            logger.warning("DingTalk HTTP client not initialized, cannot send")
            return False

        url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
        headers = {"x-acs-dingtalk-access-token": token}
        payload = {
            "robotCode": self.config.client_id,
            "userIds": [chat_id],
            "msgKey": msg_key,
            "msgParam": json.dumps(msg_param, ensure_ascii=False),
        }

        try:
            resp = await self._http.post(url, json=payload, headers=headers)
            body = resp.text
            if resp.status_code != 200:
                logger.error("DingTalk send failed msgKey={} status={} body={}", msg_key, resp.status_code, body[:500])
                return False
            try:
                result = resp.json()
            except Exception:
                result = {}
            errcode = result.get("errcode")
            if errcode not in (None, 0):
                logger.error("DingTalk send api error msgKey={} errcode={} body={}", msg_key, errcode, body[:500])
                return False
            logger.debug("DingTalk message sent to {} with msgKey={}", chat_id, msg_key)
            return True
        except Exception as e:
            logger.error("Error sending DingTalk message msgKey={} err={}", msg_key, e)
            return False

    async def _send_markdown_text(self, token: str, chat_id: str, content: str) -> bool:
        """用处，参数

        功能:
            - 发送 Markdown 文本消息。
        """
        return await self._send_batch_message(
            token,
            chat_id,
            "sampleMarkdown",
            {"text": content, "title": "Nanobot Reply"},
        )

    async def _send_media_ref(self, token: str, chat_id: str, media_ref: str) -> bool:
        """用处，参数

        功能:
            - 发送媒体引用并按需执行上传回退。
        """
        media_ref = (media_ref or "").strip()
        if not media_ref:
            return True

        upload_type = self._guess_upload_type(media_ref)
        if upload_type == "image" and self._is_http_url(media_ref):
            ok = await self._send_batch_message(
                token,
                chat_id,
                "sampleImageMsg",
                {"photoURL": media_ref},
            )
            if ok:
                return True
            logger.warning("DingTalk image url send failed, trying upload fallback: {}", media_ref)

        data, filename, content_type = await self._read_media_bytes(media_ref)
        if not data:
            logger.error("DingTalk media read failed: {}", media_ref)
            return False

        filename = filename or self._guess_filename(media_ref, upload_type)
        file_type = Path(filename).suffix.lower().lstrip(".")
        if not file_type:
            guessed = mimetypes.guess_extension(content_type or "")
            file_type = (guessed or ".bin").lstrip(".")
        if file_type == "jpeg":
            file_type = "jpg"

        media_id = await self._upload_media(
            token=token,
            data=data,
            media_type=upload_type,
            filename=filename,
            content_type=content_type,
        )
        if not media_id:
            return False

        if upload_type == "image":
            # Verified in production: sampleImageMsg accepts media_id in photoURL.
            ok = await self._send_batch_message(
                token,
                chat_id,
                "sampleImageMsg",
                {"photoURL": media_id},
            )
            if ok:
                return True
            logger.warning("DingTalk image media_id send failed, falling back to file: {}", media_ref)

        return await self._send_batch_message(
            token,
            chat_id,
            "sampleFile",
            {"mediaId": media_id, "fileName": filename, "fileType": file_type},
        )

    async def send(self, msg: OutboundMessage) -> None:
        """通过 DingTalk 频道发送一条消息。"""
        token = await self._get_access_token()
        if not token:
            return

        if msg.content and msg.content.strip():
            await self._send_markdown_text(token, msg.chat_id, msg.content.strip())

        for media_ref in msg.media or []:
            ok = await self._send_media_ref(token, msg.chat_id, media_ref)
            if ok:
                continue
            logger.error("DingTalk media send failed for {}", media_ref)
            # 提供降级的错误提示可见反馈以便问题排查
            filename = self._guess_filename(media_ref, self._guess_upload_type(media_ref))
            await self._send_markdown_text(
                token,
                msg.chat_id,
                f"[Attachment send failed: {filename}]",
            )

    async def _on_message(self, content: str, sender_id: str, sender_name: str) -> None:
        """处理传入的消息（由 NanobotDingTalkHandler 触发调用）。

        委托给 BaseChannel._handle_message()，在把消息发布至总线前会强制检查权限列表 allow_from。
        """
        try:
            logger.info("DingTalk inbound: {} from {}", content, sender_name)
            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender_id,  # For private chat, chat_id == sender_id
                content=str(content),
                metadata={
                    "sender_name": sender_name,
                    "platform": "dingtalk",
                },
            )
        except Exception as e:
            logger.error("Error publishing DingTalk message: {}", e)

#endregion
