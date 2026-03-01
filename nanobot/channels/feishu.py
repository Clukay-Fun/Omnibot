"""基于 lark-oapi SDK 且使用 WebSocket 长连接的 Feishu/Lark 频道实现。"""

import asyncio
import json
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import FeishuConfig

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateFileRequest,
        CreateFileRequestBody,
        CreateImageRequest,
        CreateImageRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        CreateMessageReactionRequest,
        CreateMessageReactionRequestBody,
        Emoji,
        GetFileRequest,
        GetMessageResourceRequest,
        P2ImMessageReceiveV1,
    )
    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None
    Emoji = None

# 消息类型展示映射
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


def _extract_share_card_content(content_json: dict, msg_type: str) -> str:
    """从分享卡片和交互式消息中提取文本内容。"""
    parts = []

    if msg_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif msg_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif msg_type == "interactive":
        parts.extend(_extract_interactive_content(content_json))
    elif msg_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif msg_type == "system":
        parts.append("[system message]")
    elif msg_type == "merge_forward":
        parts.append("[merged forward messages]")

    return "\n".join(parts) if parts else f"[{msg_type}]"


def _extract_interactive_content(content: dict) -> list[str]:
    """递归提取交互式卡片中的文本及链接内容。"""
    parts = []
    
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return [content] if content.strip() else []

    if not isinstance(content, dict):
        return parts

    if "title" in content:
        title = content["title"]
        if isinstance(title, dict):
            title_content = title.get("content", "") or title.get("text", "")
            if title_content:
                parts.append(f"title: {title_content}")
        elif isinstance(title, str):
            parts.append(f"title: {title}")

    for elements in content.get("elements", []) if isinstance(content.get("elements"), list) else []:
        for element in elements:
            parts.extend(_extract_element_content(element))

    card = content.get("card", {})
    if card:
        parts.extend(_extract_interactive_content(card))

    header = content.get("header", {})
    if header:
        header_title = header.get("title", {})
        if isinstance(header_title, dict):
            header_text = header_title.get("content", "") or header_title.get("text", "")
            if header_text:
                parts.append(f"title: {header_text}")
    
    return parts


def _extract_element_content(element: dict) -> list[str]:
    """从单一卡片元素中提取内容。"""
    parts = []
    
    if not isinstance(element, dict):
        return parts
    
    tag = element.get("tag", "")
    
    if tag in ("markdown", "lark_md"):
        content = element.get("content", "")
        if content:
            parts.append(content)

    elif tag == "div":
        text = element.get("text", {})
        if isinstance(text, dict):
            text_content = text.get("content", "") or text.get("text", "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str):
            parts.append(text)
        for field in element.get("fields", []):
            if isinstance(field, dict):
                field_text = field.get("text", {})
                if isinstance(field_text, dict):
                    c = field_text.get("content", "")
                    if c:
                        parts.append(c)

    elif tag == "a":
        href = element.get("href", "")
        text = element.get("text", "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)

    elif tag == "button":
        text = element.get("text", {})
        if isinstance(text, dict):
            c = text.get("content", "")
            if c:
                parts.append(c)
        url = element.get("url", "") or element.get("multi_url", {}).get("url", "")
        if url:
            parts.append(f"link: {url}")

    elif tag == "img":
        alt = element.get("alt", {})
        parts.append(alt.get("content", "[image]") if isinstance(alt, dict) else "[image]")

    elif tag == "note":
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    elif tag == "column_set":
        for col in element.get("columns", []):
            for ce in col.get("elements", []):
                parts.extend(_extract_element_content(ce))

    elif tag == "plain_text":
        content = element.get("content", "")
        if content:
            parts.append(content)

    else:
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))
    
    return parts


def _extract_post_content(content_json: dict) -> tuple[str, list[str]]:
    """从飞书帖子（富文本）消息内容中提取文本和图片 Keys。
    
    支持两种格式:
    1. 直接格式: {"title": "...", "content": [...]}
    2. 本地化格式: {"zh_cn": {"title": "...", "content": [...]}}
    
    返回:
        (text, image_keys) - 提取出的纯文本和图片 Key 列表
    """
    def extract_from_lang(lang_content: dict) -> tuple[str | None, list[str]]:
        if not isinstance(lang_content, dict):
            return None, []
        title = lang_content.get("title", "")
        content_blocks = lang_content.get("content", [])
        if not isinstance(content_blocks, list):
            return None, []
        text_parts = []
        image_keys = []
        if title:
            text_parts.append(title)
        for block in content_blocks:
            if not isinstance(block, list):
                continue
            for element in block:
                if isinstance(element, dict):
                    tag = element.get("tag")
                    if tag == "text":
                        text_parts.append(element.get("text", ""))
                    elif tag == "a":
                        text_parts.append(element.get("text", ""))
                    elif tag == "at":
                        text_parts.append(f"@{element.get('user_name', 'user')}")
                    elif tag == "img":
                        img_key = element.get("image_key")
                        if img_key:
                            image_keys.append(img_key)
        text = " ".join(text_parts).strip() if text_parts else None
        return text, image_keys
    
    # Try direct format first
    if "content" in content_json:
        text, images = extract_from_lang(content_json)
        if text or images:
            return text or "", images
    
    # Try localized format
    for lang_key in ("zh_cn", "en_us", "ja_jp"):
        lang_content = content_json.get(lang_key)
        text, images = extract_from_lang(lang_content)
        if text or images:
            return text or "", images
    
    return "", []


def _extract_post_text(content_json: dict) -> str:
    """从飞书帖子（富文本）消息中仅提取纯文本。
    
    遗留的针对 _extract_post_content 函数的包装器，仅返回文本。
    """
    text, _ = _extract_post_content(content_json)
    return text


# region [Feishu 频道核心类]

class FeishuChannel(BaseChannel):
    """
    基于 WebSocket 长连接的 Feishu/Lark 频道。
    
    使用 WebSocket 接收事件 - 无需公网 IP 或 webhook 暴露。
    
    依赖项:
    - 来自 Feishu 开放平台的 App ID 与 App Secret
    - 机器人已启用相关功能（Bot capability）
    - 事件订阅已开启 (im.message.receive_v1)
    """
    
    name = "feishu"
    
    def __init__(self, config: FeishuConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # 用于排重的缓存队列
        self._loop: asyncio.AbstractEventLoop | None = None
    
    async def start(self) -> None:
        """启动具有 WebSocket 长连接的 Feishu 机器人。"""
        if not FEISHU_AVAILABLE:
            logger.error("Feishu SDK not installed. Run: pip install lark-oapi")
            return
        
        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id and app_secret not configured")
            return
        
        self._running = True
        self._loop = asyncio.get_running_loop()
        
        # Create Lark client for sending messages
        self._client = lark.Client.builder() \
            .app_id(self.config.app_id) \
            .app_secret(self.config.app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()
        
        # Create event handler (only register message receive, ignore other events)
        event_handler = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(
            self._on_message_sync
        ).build()
        
        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO
        )
        
        # Start WebSocket client in a separate thread with reconnect loop
        def run_ws():
            while self._running:
                try:
                    self._ws_client.start()
                except Exception as e:
                    logger.warning("Feishu WebSocket error: {}", e)
                if self._running:
                    import time; time.sleep(5)
        
        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()
        
        logger.info("Feishu bot started with WebSocket long connection")
        logger.info("No public IP required - using WebSocket to receive events")
        
        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)
    
    async def stop(self) -> None:
        """
        停止 Feishu 机器人。

        注意: lark.ws.Client 并未暴露 stop 方法，程序正常退出即可关闭 Client。

        参考: https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/ws/client.py#L86
        """
        self._running = False
        logger.info("Feishu bot stopped")
    
    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> None:
        """用于添加表情回应（运行在线程池中）的同步辅助方法。"""
        try:
            request = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                ).build()
            
            response = self._client.im.v1.message_reaction.create(request)
            
            if not response.success():
                logger.warning("Failed to add reaction: code={}, msg={}", response.code, response.msg)
            else:
                logger.debug("Added {} reaction to message {}", emoji_type, message_id)
        except Exception as e:
            logger.warning("Error adding reaction: {}", e)

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        """
        向特定消息添加表情回应（非阻塞操作）。
        
        常见的表情类型: THUMBSUP, OK, EYES, DONE, OnIt, HEART
        """
        if not self._client or not Emoji:
            return
        
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)
    
    # Regex to match markdown tables (header + separator + data rows)
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    _CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)

    @staticmethod
    def _parse_md_table(table_text: str) -> dict | None:
        """将一段 Markdown 文本形式的表格解析并转换为 Feishu 的表格元素格式。"""
        lines = [l.strip() for l in table_text.strip().split("\n") if l.strip()]
        if len(lines) < 3:
            return None
        split = lambda l: [c.strip() for c in l.strip("|").split("|")]
        headers = split(lines[0])
        rows = [split(l) for l in lines[2:]]
        columns = [{"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
                   for i, h in enumerate(headers)]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [{f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows],
        }

    # 飞书卡片中表格数量上限（超出后以 markdown 文本形式保留）
    _MAX_CARD_TABLES = 5

    def _build_card_elements(self, content: str) -> list[dict]:
        """将文本内容拆分为包含 div/markdown 与 table 等 Feishu 卡片所需的结构化元素。"""
        elements, last_end = [], 0
        table_count = 0
        for m in self._TABLE_RE.finditer(content):
            before = content[last_end:m.start()]
            if before.strip():
                elements.extend(self._split_headings(before))
            if table_count < self._MAX_CARD_TABLES:
                parsed = self._parse_md_table(m.group(1))
                if parsed:
                    elements.append(parsed)
                    table_count += 1
                else:
                    elements.append({"tag": "markdown", "content": m.group(1)})
            else:
                # 超出限制，以 markdown 文本形式保留
                elements.append({"tag": "markdown", "content": m.group(1)})
            last_end = m.end()
        remaining = content[last_end:]
        if remaining.strip():
            elements.extend(self._split_headings(remaining))
        return elements or [{"tag": "markdown", "content": content}]

    def _split_headings(self, content: str) -> list[dict]:
        """按标题切分内容，将 Markdown 的标题格式转换为加粗的 div 元素。"""
        protected = content
        code_blocks = []
        for m in self._CODE_BLOCK_RE.finditer(content):
            code_blocks.append(m.group(1))
            protected = protected.replace(m.group(1), f"\x00CODE{len(code_blocks)-1}\x00", 1)

        elements = []
        last_end = 0
        for m in self._HEADING_RE.finditer(protected):
            before = protected[last_end:m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            text = m.group(2).strip()
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{text}**",
                },
            })
            last_end = m.end()
        remaining = protected[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})

        for i, cb in enumerate(code_blocks):
            for el in elements:
                if el.get("tag") == "markdown":
                    el["content"] = el["content"].replace(f"\x00CODE{i}\x00", cb)

        return elements or [{"tag": "markdown", "content": content}]

    def _parse_line_to_post_elements(self, line: str) -> list[dict]:
        """将单行 Markdown 文本解析为飞书富文本元素列表。"""
        elements = []
        link_re = re.compile(r"\[(.*?)\]\((.*?)\)")
        bold_re = re.compile(r"\*\*(.*?)\*\*")
        
        tokens = []
        for m in link_re.finditer(line):
            start, end = m.span()
            tokens.append((start, end, "a", m.group(1), m.group(2)))
            
        for m in bold_re.finditer(line):
            start, end = m.span()
            overlap = False
            for ts, te, tt, _, _ in tokens:
                if (start >= ts and start < te) or (end > ts and end <= te):
                    overlap = True
                    break
            if not overlap:
                tokens.append((start, end, "bold", m.group(1), None))
                
        tokens.sort()
        curr = 0
        for start, end, ttype, text, extra in tokens:
            if start > curr:
                elements.append({"tag": "text", "text": line[curr:start]})
            if ttype == "a":
                elements.append({"tag": "a", "text": text, "href": extra})
            elif ttype == "bold":
                elements.append({"tag": "text", "text": text, "style": ["bold"]})
            curr = end
        if curr < len(line):
            elements.append({"tag": "text", "text": line[curr:]})
        return elements or [{"tag": "text", "text": line}]

    def _build_post_payload(self, content: str) -> str:
        """将 Markdown 转换为飞书 post 负载。"""
        lines = content.split("\n")
        post_content = []
        for line in lines:
            if not line.strip():
                post_content.append([{"tag": "text", "text": "\n"}])
                continue
            h_match = self._HEADING_RE.match(line)
            if h_match:
                post_content.append([{"tag": "text", "text": h_match.group(2), "style": ["bold"]}])
                continue
            if "|" in line:
                post_content.append([{"tag": "text", "text": line}])
                continue
            post_content.append(self._parse_line_to_post_elements(line))
        return json.dumps({"zh_cn": {"title": "", "content": post_content}}, ensure_ascii=False)

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
    _AUDIO_EXTS = {".opus"}
    _FILE_TYPE_MAP = {
        ".opus": "opus", ".mp4": "mp4", ".pdf": "pdf", ".doc": "doc", ".docx": "doc",
        ".xls": "xls", ".xlsx": "xls", ".ppt": "ppt", ".pptx": "ppt",
    }

    def _upload_image_sync(self, file_path: str) -> str | None:
        """将图片上传至 Feishu 服务端，并返回该文件的 image_key。"""
        try:
            with open(file_path, "rb") as f:
                request = CreateImageRequest.builder() \
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image_type("message")
                        .image(f)
                        .build()
                    ).build()
                response = self._client.im.v1.image.create(request)
                if response.success():
                    image_key = response.data.image_key
                    logger.debug("Uploaded image {}: {}", os.path.basename(file_path), image_key)
                    return image_key
                else:
                    logger.error("Failed to upload image: code={}, msg={}", response.code, response.msg)
                    return None
        except Exception as e:
            logger.error("Error uploading image {}: {}", file_path, e)
            return None

    def _upload_file_sync(self, file_path: str) -> str | None:
        """将文件上传至 Feishu 服务端，并返回该文件的 file_key。"""
        ext = os.path.splitext(file_path)[1].lower()
        file_type = self._FILE_TYPE_MAP.get(ext, "stream")
        file_name = os.path.basename(file_path)
        try:
            with open(file_path, "rb") as f:
                request = CreateFileRequest.builder() \
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type(file_type)
                        .file_name(file_name)
                        .file(f)
                        .build()
                    ).build()
                response = self._client.im.v1.file.create(request)
                if response.success():
                    file_key = response.data.file_key
                    logger.debug("Uploaded file {}: {}", file_name, file_key)
                    return file_key
                else:
                    logger.error("Failed to upload file: code={}, msg={}", response.code, response.msg)
                    return None
        except Exception as e:
            logger.error("Error uploading file {}: {}", file_path, e)
            return None

    def _download_image_sync(self, message_id: str, image_key: str) -> tuple[bytes | None, str | None]:
        """根据指定的 message_id 及 image_key 下载来自 Feishu 消息的图片。"""
        try:
            request = GetMessageResourceRequest.builder() \
                .message_id(message_id) \
                .file_key(image_key) \
                .type("image") \
                .build()
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                # GetMessageResourceRequest returns BytesIO, need to read bytes
                if hasattr(file_data, 'read'):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                logger.error("Failed to download image: code={}, msg={}", response.code, response.msg)
                return None, None
        except Exception as e:
            logger.error("Error downloading image {}: {}", image_key, e)
            return None, None

    def _download_file_sync(
        self, message_id: str, file_key: str, resource_type: str = "file"
    ) -> tuple[bytes | None, str | None]:
        """依据 message_id 与 file_key 从 Feishu 消息中下载文件/音频/多媒体资源。"""
        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(resource_type)
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                logger.error("Failed to download {}: code={}, msg={}", resource_type, response.code, response.msg)
                return None, None
        except Exception:
            logger.exception("Error downloading {} {}", resource_type, file_key)
            return None, None

    async def _download_and_save_media(
        self,
        msg_type: str,
        content_json: dict,
        message_id: str | None = None
    ) -> tuple[str | None, str]:
        """
        从 Feishu 下载媒体文件并保存到本地磁盘目录。

        返回:
            (file_path, content_text) - 如果下载失败， file_path 将为 None
        """
        loop = asyncio.get_running_loop()
        media_dir = Path.home() / ".nanobot" / "media"
        media_dir.mkdir(parents=True, exist_ok=True)

        data, filename = None, None

        if msg_type == "image":
            image_key = content_json.get("image_key")
            if image_key and message_id:
                data, filename = await loop.run_in_executor(
                    None, self._download_image_sync, message_id, image_key
                )
                if not filename:
                    filename = f"{image_key[:16]}.jpg"

        elif msg_type in ("audio", "file", "media"):
            file_key = content_json.get("file_key")
            if file_key and message_id:
                data, filename = await loop.run_in_executor(
                    None, self._download_file_sync, message_id, file_key, msg_type
                )
                if not filename:
                    ext = {"audio": ".opus", "media": ".mp4"}.get(msg_type, "")
                    filename = f"{file_key[:16]}{ext}"

        if data and filename:
            file_path = media_dir / filename
            file_path.write_bytes(data)
            logger.debug("Downloaded {} to {}", msg_type, file_path)
            return str(file_path), f"[{msg_type}: {filename}]"

        return None, f"[{msg_type}: download failed]"

    def _send_message_sync(self, receive_id_type: str, receive_id: str, msg_type: str, content: str) -> bool:
        """串行化执行单一消息（文本/图像/文件/交互卡片内容）的端点发送动作。"""
        try:
            request = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                ).build()
            response = self._client.im.v1.message.create(request)
            if not response.success():
                logger.error(
                    "Failed to send Feishu {} message: code={}, msg={}, log_id={}",
                    msg_type, response.code, response.msg, response.get_log_id()
                )
                return False
            logger.debug("Feishu {} message sent to {}", msg_type, receive_id)
            return True
        except Exception as e:
            logger.error("Error sending Feishu {} message: {}", msg_type, e)
            return False

    async def send(self, msg: OutboundMessage) -> None:
        """通过 Feishu 频道将包装好的标准消息实例发送出去，如有包含媒体文件（图像/音频）也可以一并发送。"""
        if not self._client:
            logger.warning("Feishu client not initialized")
            return

        try:
            receive_id_type = "chat_id" if msg.chat_id.startswith("oc_") else "open_id"
            loop = asyncio.get_running_loop()

            for file_path in msg.media:
                if not os.path.isfile(file_path):
                    logger.warning("Media file not found: {}", file_path)
                    continue
                ext = os.path.splitext(file_path)[1].lower()
                if ext in self._IMAGE_EXTS:
                    key = await loop.run_in_executor(None, self._upload_image_sync, file_path)
                    if key:
                        await loop.run_in_executor(
                            None, self._send_message_sync,
                            receive_id_type, msg.chat_id, "image", json.dumps({"image_key": key}, ensure_ascii=False),
                        )
                else:
                    key = await loop.run_in_executor(None, self._upload_file_sync, file_path)
                    if key:
                        media_type = "audio" if ext in self._AUDIO_EXTS else "file"
                        await loop.run_in_executor(
                            None, self._send_message_sync,
                            receive_id_type, msg.chat_id, media_type, json.dumps({"file_key": key}, ensure_ascii=False),
                        )

            if msg.content and msg.content.strip():
                # 使用交互卡片格式（原生支持 Markdown 渲染）
                card = {"config": {"wide_screen_mode": True}, "elements": self._build_card_elements(msg.content)}
                await loop.run_in_executor(
                    None, self._send_message_sync,
                    receive_id_type, msg.chat_id, "interactive", json.dumps(card, ensure_ascii=False),
                )

        except Exception as e:
            logger.error("Error sending Feishu message: {}", e)
    
    def _on_message_sync(self, data: "P2ImMessageReceiveV1") -> None:
        """
        处理传入消息的同步封装处理器（受 WebSocket 工作线程调用）。
        调度至底层主事件循环池执行真正的核心逻辑。
        """
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)
    
    async def _on_message(self, data: "P2ImMessageReceiveV1") -> None:
        """真实解析 Feishu 传入消息内容的处理器。"""
        try:
            event = data.event
            message = event.message
            sender = event.sender

            # Deduplication check
            message_id = message.message_id
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None

            # Trim cache
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

            # Skip bot messages
            if sender.sender_type == "bot":
                return

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type
            msg_type = message.message_type

            # Add reaction
            await self._add_reaction(message_id, self.config.react_emoji)

            # Parse content
            content_parts = []
            media_paths = []

            try:
                content_json = json.loads(message.content) if message.content else {}
            except json.JSONDecodeError:
                content_json = {}

            if msg_type == "text":
                text = content_json.get("text", "")
                if text:
                    content_parts.append(text)

            elif msg_type == "post":
                text, image_keys = _extract_post_content(content_json)
                if text:
                    content_parts.append(text)
                # Download images embedded in post
                for img_key in image_keys:
                    file_path, content_text = await self._download_and_save_media(
                        "image", {"image_key": img_key}, message_id
                    )
                    if file_path:
                        media_paths.append(file_path)
                    content_parts.append(content_text)

            elif msg_type in ("image", "audio", "file", "media"):
                file_path, content_text = await self._download_and_save_media(msg_type, content_json, message_id)
                if file_path:
                    media_paths.append(file_path)
                content_parts.append(content_text)

            elif msg_type in ("share_chat", "share_user", "interactive", "share_calendar_event", "system", "merge_forward"):
                # Handle share cards and interactive messages
                text = _extract_share_card_content(content_json, msg_type)
                if text:
                    content_parts.append(text)

            else:
                content_parts.append(MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

            content = "\n".join(content_parts) if content_parts else ""

            if not content and not media_paths:
                return

            # Forward to message bus
            reply_to = chat_id if chat_type == "group" else sender_id
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                media=media_paths,
                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "msg_type": msg_type,
                }
            )

        except Exception as e:
            logger.error("Error processing Feishu message: {}", e)

# endregion
