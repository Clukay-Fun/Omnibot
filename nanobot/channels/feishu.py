"""描述:
主要功能:
    - 提供基于 lark-oapi 的 Feishu/Lark 频道收发实现。
"""

import asyncio
import json
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from nanobot.agent.memory import MemoryStore
from nanobot.agent.runtime_texts import RuntimeTextCatalog
from nanobot.agent.skill_runtime import BitableReminderRuleEngine, ReminderRuntime
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import FeishuConfig, FeishuDataConfig
from nanobot.cron.service import CronService
from nanobot.storage.audit import AuditSink
from nanobot.storage.sqlite_store import SQLiteConnectionOptions, SQLiteStore

try:
    import lark_oapi as lark
    from lark_oapi.api.cardkit.v1 import (
        ContentCardElementRequest,
        ContentCardElementRequestBody,
        IdConvertCardRequest,
        IdConvertCardRequestBody,
    )
    from lark_oapi.api.im.v1 import (
        CreateFileRequest,
        CreateFileRequestBody,
        CreateImageRequest,
        CreateImageRequestBody,
        CreateMessageReactionRequest,
        CreateMessageReactionRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        DeleteMessageRequest,
        Emoji,
        GetMessageResourceRequest,
        P2ImMessageReceiveV1,
        PatchMessageRequest,
        PatchMessageRequestBody,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
        UpdateMessageRequest,
        UpdateMessageRequestBody,
    )
    FEISHU_AVAILABLE = True
    CARDKIT_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    CARDKIT_AVAILABLE = False
    lark = None
    Emoji = None
    UpdateMessageRequest = None
    UpdateMessageRequestBody = None
    PatchMessageRequest = None
    PatchMessageRequestBody = None
    DeleteMessageRequest = None
    ReplyMessageRequest = None
    ReplyMessageRequestBody = None
    ContentCardElementRequest = None
    ContentCardElementRequestBody = None
    IdConvertCardRequest = None
    IdConvertCardRequestBody = None


@dataclass
class _FeishuStreamState:
    source_message_id: str
    bot_message_id: str
    stream_uuid: str
    sequence: int = 0
    card_id: str | None = None
    thinking_text: str = ""
    answer_text: str = ""
    thinking_collapsed: bool = False
    reply_in_thread: bool = False
    last_update_at: float = 0.0
    updated_at: float = 0.0


_STREAM_THINKING_ELEMENT_ID = "thinking_text"
_STREAM_ANSWER_ELEMENT_ID = "answer_text"

_MENTION_MARKER_RE = re.compile(r"@_user_\d+")
_AT_TAG_RE = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)

# 消息类型展示映射
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


def _safe_get(source: Any, key: str, default: Any = None) -> Any:
    """统一读取 dict/object 字段，失败时返回默认值。"""
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def _safe_dig(source: Any, *keys: str, default: Any = None) -> Any:
    """按路径读取 dict/object 嵌套字段。"""
    current = source
    for key in keys:
        current = _safe_get(current, key, None)
        if current is None:
            return default
    return current


def _safe_json_loads(value: Any) -> Any:
    """对字符串执行 JSON 解析，失败则返回原值。"""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _to_plain_data(value: Any, *, _depth: int = 0) -> Any:
    """将 SDK 对象转换为仅包含基础类型的结构。"""
    if _depth >= 6:
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_plain_data(v, _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_plain_data(v, _depth=_depth + 1) for v in value]
    if hasattr(value, "__dict__"):
        return {
            str(k): _to_plain_data(v, _depth=_depth + 1)
            for k, v in vars(value).items()
            if not k.startswith("_")
        }
    return str(value)


def _safe_json_dumps(value: Any) -> str:
    """安全序列化任意值为 JSON 字符串。"""
    plain_value = _to_plain_data(value)
    try:
        return json.dumps(plain_value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(plain_value)


def _extract_action_key(action_value: Any) -> str | None:
    """从 action value 中提取可读动作 key。"""
    if isinstance(action_value, str):
        parsed = _safe_json_loads(action_value)
        if isinstance(parsed, str):
            return parsed.strip() or None
        action_value = parsed

    if not isinstance(action_value, dict):
        return None

    for key in ("action_key", "key", "action", "value", "name", "id"):
        candidate = action_value.get(key)
        if isinstance(candidate, (str, int, float)):
            text = str(candidate).strip()
            if text:
                return text

    if len(action_value) == 1:
        first_key = next(iter(action_value.keys()), None)
        if isinstance(first_key, str) and first_key.strip():
            return first_key.strip()

    return None


def _build_card_action_content(action: Any) -> tuple[str, str | None, str | None]:
    """抽取卡片回调中的动作信息并组装为入站文本。"""
    action_tag = _safe_get(action, "tag", None)
    action_name_raw = _safe_get(action, "name", None)
    action_name = str(action_name_raw).strip() if isinstance(action_name_raw, (str, int, float)) else None
    if action_name == "":
        action_name = None

    action_value = _safe_json_loads(_safe_get(action, "value", None))
    form_value = _safe_json_loads(_safe_get(action, "form_value", None))
    option = _safe_json_loads(_safe_get(action, "option", None))

    action_key = _extract_action_key(action_value)
    if not action_key and action_name:
        action_key = action_name
    if not action_key and isinstance(form_value, dict) and len(form_value) == 1:
        first_key = next(iter(form_value.keys()), None)
        if isinstance(first_key, str) and first_key.strip():
            action_key = first_key.strip()

    parts = ["[feishu card action trigger]"]
    if action_tag:
        parts.append(f"action_tag: {action_tag}")
    if action_name:
        parts.append(f"action_name: {action_name}")
    if action_key:
        parts.append(f"action_key: {action_key}")
    if action_value not in (None, "", {}, []):
        parts.append(f"action_value: {_safe_json_dumps(action_value)}")
    if form_value not in (None, "", {}, []):
        parts.append(f"form_value: {_safe_json_dumps(form_value)}")
    if option not in (None, "", {}, []):
        parts.append(f"option: {_safe_json_dumps(option)}")

    return "\n".join(parts), action_key, action_tag


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
    1. 直接格式: {"title": "...", "content": [...]}。
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


#region Feishu频道核心类

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

    def __init__(
        self,
        config: FeishuConfig,
        bus: MessageBus,
        workspace: Path | None = None,
        feishu_data_config: FeishuDataConfig | None = None,
        state_db_path: Path | None = None,
        sqlite_options: SQLiteConnectionOptions | None = None,
    ):
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self.workspace = workspace or Path.home() / ".nanobot" / "workspace"
        self.feishu_data_config = feishu_data_config or FeishuDataConfig()
        self._runtime_text = RuntimeTextCatalog.load(self.workspace)
        self._memory = MemoryStore(self.workspace)
        self._continuation_commands = {
            cmd.strip()
            for cmd in self._runtime_text.routing_list(
                "pagination_triggers", "continuation_commands", ["continue", "more"]
            )
            if cmd.strip()
        }
        self._thinking_collapsed_summary = self._runtime_text.prompt_text(
            "progress", "thinking_collapsed_summary", "思考完成"
        )
        self._thinking_active = self._runtime_text.prompt_text("progress", "thinking_active", "思考中")
        self._thinking_placeholder_markdown = self._runtime_text.prompt_text(
            "progress", "thinking_placeholder_markdown", "> 思考中"
        )
        generic_lines = self._runtime_text.prompt_lines(
            "progress",
            "thinking_generic_lines",
            ["思考中", "思考完成", "正在思考中..."],
        )
        self._thinking_generic_lines = {line.strip() for line in generic_lines if line.strip()}
        self._thinking_generic_base = {
            re.sub(r"[。\.!！?？…]+$", "", line.strip())
            for line in self._thinking_generic_lines
            if line.strip()
        }
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # 用于排重的缓存队列
        self._recent_message_fingerprints: OrderedDict[str, float] = OrderedDict()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream_states: dict[str, _FeishuStreamState] = {}
        self._sqlite = SQLiteStore(
            state_db_path or (self.workspace / "memory" / "feishu" / "state.sqlite3"),
            options=sqlite_options,
        )
        self._audit_sink = AuditSink(
            self._sqlite,
            cleanup_interval_seconds=float(
                getattr(self.config, "audit_cleanup_interval_seconds", AuditSink.DEFAULT_CLEANUP_INTERVAL_SECONDS)
            ),
            event_audit_retention_days=int(
                getattr(self.config, "audit_event_retention_days", AuditSink.DEFAULT_EVENT_AUDIT_RETENTION_DAYS)
            ),
            feishu_message_index_retention_days=int(
                getattr(
                    self.config,
                    "audit_message_index_retention_days",
                    AuditSink.DEFAULT_FEISHU_MESSAGE_INDEX_RETENTION_DAYS,
                )
            ),
        )
        self._sqlite.migrate_legacy_feishu_json(self.workspace)
        self._message_index: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._event_registration_report: list[dict[str, Any]] = []
        self._cron_service = CronService(self.workspace / "cron_jobs.json")
        self._reminder_runtime = ReminderRuntime(self.workspace / "reminders.json")
        self._bitable_engine = BitableReminderRuleEngine(
            self.workspace,
            reminder_runtime=self._reminder_runtime,
            cron_service=self._cron_service,
            feishu_data_config=self.feishu_data_config,
        )
        self._load_message_index()

    def _read_state(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}

        welcomed_rows = self._sqlite.list_feishu_chat_state(SQLiteStore.GLOBAL_CHAT_ID, prefix="welcomed:")
        if welcomed_rows:
            payload["welcomed"] = {
                key.split(":", 1)[1]: value
                for key, value in welcomed_rows.items()
                if key.startswith("welcomed:")
            }

        group_welcomes = self._sqlite.list_feishu_state_by_key("group_welcome_last_sent")
        if group_welcomes:
            payload["group_welcomes"] = group_welcomes

        event_report = self._sqlite.get_feishu_chat_state(
            SQLiteStore.GLOBAL_CHAT_ID,
            "event_registration_report",
            default=[],
        )
        if isinstance(event_report, list) and event_report:
            payload["event_registration_report"] = event_report
        return payload

    def _write_state(self, payload: dict[str, Any]) -> None:
        welcomed = payload.get("welcomed") if isinstance(payload.get("welcomed"), dict) else {}
        for key, value in welcomed.items():
            self._sqlite.upsert_feishu_chat_state(SQLiteStore.GLOBAL_CHAT_ID, f"welcomed:{key}", value)

        group_welcomes = payload.get("group_welcomes") if isinstance(payload.get("group_welcomes"), dict) else {}
        for chat_id, value in group_welcomes.items():
            self._sqlite.upsert_feishu_chat_state(str(chat_id), "group_welcome_last_sent", value)

        event_report = payload.get("event_registration_report")
        if isinstance(event_report, list):
            self._sqlite.upsert_feishu_chat_state(
                SQLiteStore.GLOBAL_CHAT_ID,
                "event_registration_report",
                event_report,
            )

    def _load_message_index(self) -> None:
        self._sqlite.trim_feishu_message_index(1000)
        rows = self._sqlite.list_feishu_message_index(limit=1000)
        self._message_index = OrderedDict((row["message_id"], row) for row in rows)

    def _trim_message_index(self) -> None:
        self._sqlite.trim_feishu_message_index(1000)
        while len(self._message_index) > 1000:
            message_id, _ = self._message_index.popitem(last=False)
            self._sqlite.delete_feishu_message_index(message_id)

    def _persist_message_index(self) -> None:
        self._sqlite.trim_feishu_message_index(1000)

    def _persist_event_registration_report(self) -> None:
        state = self._read_state()
        state["event_registration_report"] = self._event_registration_report
        self._write_state(state)

    def _mark_welcome_sent(self, key: str) -> bool:
        state = self._read_state()
        raw_welcomed = state.get("welcomed")
        welcomed: dict[str, Any] = raw_welcomed if isinstance(raw_welcomed, dict) else {}
        if key in welcomed:
            return False
        welcomed[key] = datetime.now().isoformat()
        state["welcomed"] = welcomed
        self._write_state(state)
        return True

    def _group_welcome_allowed(self, chat_id: str) -> bool:
        state = self._read_state()
        raw_group_welcomes = state.get("group_welcomes")
        group_welcomes: dict[str, Any] = raw_group_welcomes if isinstance(raw_group_welcomes, dict) else {}
        last_sent = group_welcomes.get(chat_id)
        now = time.time()
        if isinstance(last_sent, (int, float)) and now - float(last_sent) < 24 * 60 * 60:
            return False
        group_welcomes[chat_id] = now
        state["group_welcomes"] = group_welcomes
        self._write_state(state)
        return True

    @staticmethod
    def _summarize_message_text(content: str) -> str:
        text = re.sub(r"\s+", " ", str(content or "")).strip()
        if len(text) <= 240:
            return text
        return text[:237] + "..."

    def _remember_bot_message(self, message_id: str | None, *, content: str, chat_id: str, source_message_id: str | None = None) -> None:
        if not message_id:
            return
        entry = {
            "content": self._summarize_message_text(content),
            "chat_id": chat_id,
            "source_message_id": source_message_id,
            "created_at": datetime.now().isoformat(),
        }
        self._message_index[str(message_id)] = entry
        self._sqlite.upsert_feishu_message_index(
            str(message_id),
            chat_id=str(entry.get("chat_id") or ""),
            content=str(entry.get("content") or ""),
            source_message_id=str(entry.get("source_message_id") or "") or None,
            created_at=str(entry.get("created_at") or datetime.now().isoformat()),
        )
        self._trim_message_index()

    def _resolve_quoted_bot_summary(self, metadata: dict[str, Any]) -> str:
        current_chat_id = str(metadata.get("chat_id") or "").strip()
        for key in ("upper_message_id", "parent_id", "root_id"):
            message_id = str(metadata.get(key) or "").strip()
            if not message_id:
                continue
            if message_id not in self._message_index:
                self._load_message_index()
            entry = self._message_index.get(message_id)
            if not entry:
                continue
            entry_chat_id = str(entry.get("chat_id") or "").strip()
            if current_chat_id and entry_chat_id and current_chat_id != entry_chat_id:
                continue
            content = str(entry.get("content") or "").strip()
            if content:
                return content
        return ""

    def _resolve_activation_policy(self, *, chat_type: str, is_topic: bool) -> str:
        if chat_type != "group":
            return str(self.config.activation_private_policy or "always").lower()
        if is_topic:
            return str(self.config.activation_topic_policy or "always").lower()
        return str(self.config.activation_group_policy or "mention").lower()

    @staticmethod
    def _is_topic_message(message: Any) -> bool:
        thread_id = _safe_get(message, "thread_id", None)
        if thread_id:
            return True
        root_id = _safe_get(message, "root_id", None)
        parent_id = _safe_get(message, "parent_id", None)
        message_id = _safe_get(message, "message_id", None)
        return bool(root_id and parent_id and message_id and parent_id != message_id)

    def _has_admin_prefix_bypass(self, *, sender_id: str, content: str) -> bool:
        prefix = str(self.config.activation_admin_prefix_bypass or "").strip()
        if not prefix or not content:
            return False
        if sender_id not in set(self.config.activation_admin_open_ids or []):
            return False
        return content.lstrip().startswith(prefix)

    @staticmethod
    def _is_mentioned(event_message: Any, *, content_json: dict[str, Any], raw_content: str, text: str) -> bool:
        payload_mentions = _safe_get(event_message, "mentions", None)
        if isinstance(payload_mentions, list) and payload_mentions:
            return True
        json_mentions = content_json.get("mentions")
        if isinstance(json_mentions, list) and json_mentions:
            return True
        if _MENTION_MARKER_RE.search(text):
            return True
        return bool(_AT_TAG_RE.search(raw_content))

    def _is_continuation_command(self, text: str) -> bool:
        return text.strip() in self._continuation_commands

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
        await self._audit_sink.start()
        await self._cron_service.start()

        # Create Lark client for sending messages
        self._client = lark.Client.builder() \
            .app_id(self.config.app_id) \
            .app_secret(self.config.app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()

        # Create event handler (message receive + optional access-event no-op)
        event_handler_builder = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(
            self._on_message_sync
        )

        event_handler_builder = self._register_optional_event(
            event_handler_builder,
            ["register_p2_im_chat_access_event_bot_p2p_chat_entered_v1"],
            lambda _event: None,
        )
        event_handler_builder = self._register_optional_event(
            event_handler_builder,
            ["register_p2_card_action_trigger"],
            self._on_card_action_sync,
        )
        event_handler_builder = self._register_optional_event(
            event_handler_builder,
            ["register_p2_im_message_read_v1"],
            self._on_message_read_sync,
        )
        event_handler_builder = self._register_optional_event(
            event_handler_builder,
            ["register_p2_im_chat_member_user_added_v1"],
            self._on_chat_member_added_sync,
        )
        event_handler_builder = self._register_optional_event(
            event_handler_builder,
            ["register_p2_im_chat_create_v1", "register_p2_im_p2p_chat_create_v1"],
            self._on_p2p_chat_create_sync,
        )
        event_handler_builder = self._register_optional_event(
            event_handler_builder,
            ["register_p2_drive_file_bitable_field_changed_v1"],
            self._on_bitable_field_changed_sync,
        )
        event_handler_builder = self._register_optional_event(
            event_handler_builder,
            ["register_p2_drive_file_bitable_record_changed_v1", "register_p2_drive_file_bitable_record_change_v1"],
            self._on_bitable_record_changed_sync,
        )

        event_handler = event_handler_builder.build()

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
                    time.sleep(5)

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
        await self._audit_sink.stop()
        self._cron_service.stop()
        logger.info("Feishu bot stopped")

    def _register_optional_event(self, builder: Any, names: list[str], handler: Any) -> Any:
        for name in names:
            register = getattr(builder, name, None)
            if callable(register):
                logger.info("Feishu optional event registered: {}", name)
                self._event_registration_report.append({
                    "requested": names,
                    "method": name,
                    "status": "registered",
                })
                self._persist_event_registration_report()
                return register(handler)
        logger.debug("Feishu optional event not available: {}", "/".join(names))
        self._event_registration_report.append({
            "requested": names,
            "method": None,
            "status": "skipped",
        })
        self._persist_event_registration_report()
        return builder

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
        lines = [line.strip() for line in table_text.strip().split("\n") if line.strip()]
        if len(lines) < 3:
            return None

        def split_row(text: str) -> list[str]:
            return [cell.strip() for cell in text.strip("|").split("|")]

        headers = split_row(lines[0])
        rows = [split_row(line) for line in lines[2:]]
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
        ok, _ = self._send_message_detail_sync(receive_id_type, receive_id, msg_type, content)
        return ok

    def _send_message_detail_sync(
        self,
        receive_id_type: str,
        receive_id: str,
        msg_type: str,
        content: str,
    ) -> tuple[bool, str | None]:
        """发送消息并在成功时返回 message_id。"""
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
                return False, None
            logger.debug("Feishu {} message sent to {}", msg_type, receive_id)
            message_id = getattr(getattr(response, "data", None), "message_id", None)
            return True, message_id
        except Exception as e:
            logger.error("Error sending Feishu {} message: {}", msg_type, e)
            return False, None

    @staticmethod
    def _is_thread_context(metadata: dict[str, Any] | None) -> bool:
        """判断元数据是否来自话题上下文。"""
        if not metadata:
            return False
        if metadata.get("thread_id"):
            return True
        root_id = metadata.get("root_id")
        parent_id = metadata.get("parent_id")
        message_id = metadata.get("message_id")
        return bool(root_id and parent_id and message_id and parent_id != message_id)

    def _resolve_reply_in_thread(self, metadata: dict[str, Any] | None) -> bool:
        """按优先级解析 reply_in_thread。"""
        if metadata is None:
            return bool(self.config.reply_in_thread)
        if "_reply_in_thread" in metadata:
            return bool(metadata.get("_reply_in_thread"))
        if metadata.get("_start_topic_session"):
            return True
        if self._is_thread_context(metadata):
            return True
        return bool(self.config.reply_in_thread)

    def _reply_message_detail_sync(
        self,
        message_id: str,
        msg_type: str,
        content: str,
        reply_in_thread: bool,
    ) -> tuple[bool, str | None]:
        """回复指定消息并在成功时返回 message_id。"""
        if not ReplyMessageRequest or not ReplyMessageRequestBody:
            return False, None

        try:
            request = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type(msg_type)
                    .content(content)
                    .reply_in_thread(reply_in_thread)
                    .build()
                ).build()
            response = self._client.im.v1.message.reply(request)
            if not response.success():
                logger.warning(
                    "Failed to reply Feishu {} message {}: code={}, msg={}",
                    msg_type,
                    message_id,
                    response.code,
                    response.msg,
                )
                return False, None
            replied_id = getattr(getattr(response, "data", None), "message_id", None)
            return True, replied_id
        except Exception as e:
            logger.warning("Error replying Feishu {} message {}: {}", msg_type, message_id, e)
            return False, None

    def _delete_message_sync(self, message_id: str) -> bool:
        """删除既有 Feishu 消息。"""
        if not DeleteMessageRequest:
            return False
        try:
            request = DeleteMessageRequest.builder().message_id(message_id).build()
            response = self._client.im.v1.message.delete(request)
            if not response.success():
                logger.warning(
                    "Failed to delete Feishu message {}: code={}, msg={}",
                    message_id,
                    response.code,
                    response.msg,
                )
                return False
            return True
        except Exception as e:
            logger.warning("Error deleting Feishu message {}: {}", message_id, e)
            return False

    def _update_message_put_sync(self, message_id: str, content: str, msg_type: str | None) -> bool:
        """通过 im.v1.message.update (PUT) 更新消息。"""
        if not UpdateMessageRequest or not UpdateMessageRequestBody:
            return False
        try:
            body_builder = UpdateMessageRequestBody.builder().content(content)
            if msg_type:
                body_builder = body_builder.msg_type(msg_type)
            request = UpdateMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(body_builder.build()) \
                .build()
            response = self._client.im.v1.message.update(request)
            if not response.success():
                logger.warning(
                    "Failed to update Feishu message {} via PUT: code={}, msg={}",
                    message_id,
                    response.code,
                    response.msg,
                )
                return False
            return True
        except Exception as e:
            logger.warning("Error updating Feishu message {} via PUT: {}", message_id, e)
            return False

    def _update_message_patch_sync(self, message_id: str, content: str) -> bool:
        """通过 im.v1.message.patch 作为兜底更新消息。"""
        if not PatchMessageRequest or not PatchMessageRequestBody:
            return False
        try:
            request = PatchMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(PatchMessageRequestBody.builder().content(content).build()) \
                .build()
            response = self._client.im.v1.message.patch(request)
            if not response.success():
                logger.warning(
                    "Failed to update Feishu message {} via PATCH: code={}, msg={}",
                    message_id,
                    response.code,
                    response.msg,
                )
                return False
            return True
        except Exception as e:
            logger.warning("Error updating Feishu message {} via PATCH: {}", message_id, e)
            return False

    def _update_message_sync(self, message_id: str, msg_type: str, content: str) -> bool:
        """更新既有 Feishu 消息内容，包含多级降级。"""
        if msg_type == "interactive":
            if self._update_message_patch_sync(message_id, content):
                return True
            return self._update_message_put_sync(message_id, content, None)

        # 1) 优先按规范携带 msg_type 调用 PUT
        if self._update_message_put_sync(message_id, content, msg_type):
            return True
        # 2) 部分场景下 PUT + msg_type 会被拒绝，重试不带 msg_type
        if self._update_message_put_sync(message_id, content, None):
            return True
        # 3) 最后退化到 PATCH
        return self._update_message_patch_sync(message_id, content)

    def _convert_message_id_to_card_id_sync(self, message_id: str) -> str | None:
        """将消息 ID 转换为 CardKit card_id。"""
        if not CARDKIT_AVAILABLE or not IdConvertCardRequest or not IdConvertCardRequestBody:
            return None
        try:
            request = IdConvertCardRequest.builder().request_body(
                IdConvertCardRequestBody.builder().message_id(message_id).build()
            ).build()
            response = self._client.cardkit.v1.card.id_convert(request)
            if not response.success():
                logger.warning(
                    "CardKit id_convert failed for message {}: code={}, msg={}",
                    message_id,
                    response.code,
                    response.msg,
                )
                return None
            return getattr(getattr(response, "data", None), "card_id", None)
        except Exception as e:
            logger.warning("CardKit id_convert error for message {}: {}", message_id, e)
            return None

    def _build_interactive_card_content(self, content: str) -> str:
        """将文本内容转换为交互卡片 content JSON。"""
        normalized = self._normalize_markdown_headings(content)
        card = {"config": {"wide_screen_mode": True}, "elements": self._build_card_elements(normalized)}
        return json.dumps(card, ensure_ascii=False)

    @staticmethod
    def _normalize_markdown_headings(content: str) -> str:
        """将 Markdown 标题行降级为普通文本粗体，避免频道端大标题样式。"""
        if not content:
            return content

        lines = content.splitlines()
        out: list[str] = []
        in_code_block = False

        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                out.append(line)
                continue

            if not in_code_block:
                candidate = stripped
                if candidate.startswith("#"):
                    text = candidate.lstrip("#").strip()
                    out.append(f"**{text}**" if text else "")
                    continue

            out.append(line)

        return "\n".join(out)

    def _format_thinking_block(self, thinking_text: str, collapsed: bool) -> str:
        """格式化思考区文本（浅色/小块风格）。"""
        summary = self._thinking_collapsed_summary if collapsed else self._thinking_active
        if not self.config.stream_card_show_thinking:
            return ""

        detail_lines = self._extract_specific_thinking_lines(thinking_text)
        if not detail_lines:
            return ""

        prefix = "> "
        quoted_lines = [f"{prefix}{summary}"]
        for line in detail_lines:
            quoted_lines.append(f"{prefix}{line}")
        return "\n".join(quoted_lines)

    @staticmethod
    def _normalize_thinking_line(line: str) -> str:
        """规范化思考文本单行内容，用于占位词判定。"""
        normalized = line.strip()
        if not normalized:
            return ""
        normalized = re.sub(r"[。\.！!？?…]+$", "", normalized)
        return normalized.strip()

    def _is_generic_thinking_line(self, line: str) -> bool:
        """判断是否为通用占位思考文本。"""
        normalized = self._normalize_thinking_line(line)
        if not normalized:
            return True
        return normalized in self._thinking_generic_base or normalized in self._thinking_generic_lines

    def _extract_specific_thinking_lines(self, thinking_text: str | None) -> list[str]:
        """提取可展示的具体思考行，自动过滤占位词。"""
        if not thinking_text:
            return []
        lines = [line.strip() for line in str(thinking_text).splitlines() if line.strip()]
        return [line for line in lines if not self._is_generic_thinking_line(line)]

    def _has_specific_thinking_content(self, thinking_text: str | None) -> bool:
        """判断是否包含可展示的具体思考内容。"""
        return bool(self._extract_specific_thinking_lines(thinking_text))

    def _build_streaming_body_elements(self, thinking_content: str, answer_content: str) -> list[dict[str, Any]]:
        """构造流式卡片 body elements。"""
        if not self.config.stream_card_show_thinking:
            return [
                {
                    "tag": "markdown",
                    "element_id": _STREAM_ANSWER_ELEMENT_ID,
                    "content": answer_content,
                }
            ]

        # 始终保留 thinking 元素，避免后续 CardKit 增量更新找不到 element_id。
        if not thinking_content.strip():
            thinking_content = self._thinking_placeholder_markdown

        return [
            {
                "tag": "markdown",
                "element_id": _STREAM_THINKING_ELEMENT_ID,
                "content": thinking_content,
            },
            {
                "tag": "markdown",
                "content": "---",
            },
            {
                "tag": "markdown",
                "element_id": _STREAM_ANSWER_ELEMENT_ID,
                "content": answer_content,
            },
        ]

    def _build_streaming_initial_card_content(self, thinking_text: str, answer_text: str, collapsed: bool) -> str:
        """构造首条 Card 2.0 流式卡片。"""
        thinking_content = self._format_thinking_block(thinking_text, collapsed)
        answer_content = self._normalize_markdown_headings(answer_text)
        print_frequency_ms = max(30, int(self.config.stream_card_print_frequency_ms))
        print_step = max(1, int(self.config.stream_card_print_step))
        print_strategy = self.config.stream_card_print_strategy
        summary_text = self.config.stream_card_summary
        header_title = (self.config.stream_card_header_title or "").strip()
        card = {
            "schema": "2.0",
            "config": {
                "streaming_mode": True,
                "summary": {"content": summary_text},
                "streaming_config": {
                    "print_frequency_ms": {
                        "default": print_frequency_ms,
                        "android": print_frequency_ms,
                        "ios": print_frequency_ms,
                        "pc": print_frequency_ms,
                    },
                    "print_step": {
                        "default": print_step,
                        "android": print_step,
                        "ios": print_step,
                        "pc": print_step,
                    },
                    "print_strategy": print_strategy,
                },
            },
            "body": {
                "elements": self._build_streaming_body_elements(thinking_content, answer_content),
            },
        }
        if header_title:
            card["header"] = {"title": {"content": header_title, "tag": "plain_text"}}
        return json.dumps(card, ensure_ascii=False)

    def _build_streaming_update_card_content(self, thinking_text: str, answer_text: str, collapsed: bool) -> str:
        """构造 Card 2.0 更新内容（用于消息级回退更新）。"""
        thinking_content = self._format_thinking_block(thinking_text, collapsed)
        answer_content = self._normalize_markdown_headings(answer_text)
        card = {
            "schema": "2.0",
            "body": {
                "elements": self._build_streaming_body_elements(thinking_content, answer_content),
            },
        }
        return json.dumps(card, ensure_ascii=False)

    def _update_cardkit_element_text_sync(
        self,
        card_id: str,
        element_id: str,
        content: str,
        request_uuid: str,
        sequence: int,
    ) -> bool:
        """通过 CardKit 组件 content 接口更新文本元素，触发打字机效果。"""
        if not CARDKIT_AVAILABLE or not ContentCardElementRequest or not ContentCardElementRequestBody:
            return False

        card_element_resource = getattr(getattr(self._client.cardkit.v1, "card_element", None), "content", None)
        if card_element_resource is None:
            return False

        try:
            request = ContentCardElementRequest.builder() \
                .card_id(card_id) \
                .element_id(element_id) \
                .request_body(
                    ContentCardElementRequestBody.builder()
                    .content(content)
                    .uuid(request_uuid)
                    .sequence(sequence)
                    .build()
                ).build()
            response = self._client.cardkit.v1.card_element.content(request)
            if not response.success():
                logger.warning(
                    "CardKit content update failed for card {}: code={}, msg={}",
                    card_id,
                    response.code,
                    response.msg,
                )
                return False
            return True
        except Exception as e:
            logger.warning("CardKit content update error for card {}: {}", card_id, e)
            return False

    def _cleanup_stream_states(self) -> None:
        """清理超时未更新的流式卡片状态。"""
        ttl_seconds = max(1, int(self.config.stream_card_ttl_seconds))
        now = time.monotonic()
        stale_keys = [
            source_id
            for source_id, state in self._stream_states.items()
            if now - state.updated_at > ttl_seconds
        ]
        for source_id in stale_keys:
            self._stream_states.pop(source_id, None)

    async def _try_enable_cardkit_for_state(
        self,
        loop: asyncio.AbstractEventLoop,
        state: _FeishuStreamState,
    ) -> None:
        """尝试为当前状态绑定 CardKit card_id。"""

        card_id = await loop.run_in_executor(None, self._convert_message_id_to_card_id_sync, state.bot_message_id)
        if not card_id:
            return

        state.card_id = card_id

    async def _create_stream_state(
        self,
        loop: asyncio.AbstractEventLoop,
        source_message_id: str,
        receive_id_type: str,
        chat_id: str,
        thinking_text: str,
        answer_text: str,
        thinking_collapsed: bool,
        reply_in_thread: bool,
    ) -> _FeishuStreamState | None:
        """创建首条流式卡片消息并初始化状态。"""
        card_payload = self._build_streaming_initial_card_content(thinking_text, answer_text, thinking_collapsed)

        ok = False
        bot_message_id: str | None = None
        if self.config.reply_to_message:
            ok, bot_message_id = await loop.run_in_executor(
                None,
                self._reply_message_detail_sync,
                source_message_id,
                "interactive",
                card_payload,
                reply_in_thread,
            )

        if not ok:
            ok, bot_message_id = await loop.run_in_executor(
                None,
                self._send_message_detail_sync,
                receive_id_type,
                chat_id,
                "interactive",
                card_payload,
            )

        if not ok or not bot_message_id:
            return None

        now = time.monotonic()
        state = _FeishuStreamState(
            source_message_id=source_message_id,
            bot_message_id=bot_message_id,
            stream_uuid=uuid4().hex,
            thinking_text=thinking_text,
            answer_text=answer_text,
            thinking_collapsed=thinking_collapsed,
            reply_in_thread=reply_in_thread,
            last_update_at=now,
            updated_at=now,
        )

        await self._try_enable_cardkit_for_state(loop, state)

        self._stream_states[source_message_id] = state
        self._remember_bot_message(bot_message_id, content=answer_text or thinking_text, chat_id=chat_id, source_message_id=source_message_id)
        return state

    async def _update_stream_card(
        self,
        loop: asyncio.AbstractEventLoop,
        state: _FeishuStreamState,
        *,
        update_thinking: bool,
        update_answer: bool,
        allow_message_fallback: bool,
    ) -> bool:
        """更新单个流式卡片，优先 CardKit 2.0，失败退化到 IM message.update。"""
        updates: list[tuple[str, str]] = []
        if update_thinking:
            updates.append((
                _STREAM_THINKING_ELEMENT_ID,
                self._format_thinking_block(state.thinking_text, state.thinking_collapsed),
            ))
        if update_answer:
            updates.append((
                _STREAM_ANSWER_ELEMENT_ID,
                self._normalize_markdown_headings(state.answer_text),
            ))

        if not updates:
            return True

        if state.card_id:
            cardkit_all_ok = True
            for element_id, element_content in updates:
                next_sequence = state.sequence + 1
                content_ok = False
                for _ in range(2):
                    content_ok = await loop.run_in_executor(
                        None,
                        self._update_cardkit_element_text_sync,
                        state.card_id,
                        element_id,
                        element_content,
                        uuid4().hex,
                        next_sequence,
                    )
                    if content_ok:
                        break
                if not content_ok:
                    cardkit_all_ok = False
                    break
                state.sequence = next_sequence
            if cardkit_all_ok:
                return True

        if allow_message_fallback:
            card_payload = self._build_streaming_update_card_content(
                state.thinking_text,
                state.answer_text,
                state.thinking_collapsed,
            )
            im_ok = await loop.run_in_executor(
                None,
                self._update_message_sync,
                state.bot_message_id,
                "interactive",
                card_payload,
            )
            if im_ok:
                state.sequence += 1
                return True
        return False

    async def _send_streaming_content(
        self,
        msg: OutboundMessage,
        receive_id_type: str,
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        """发送/更新飞书流式单卡内容；返回是否已处理。"""
        metadata = msg.metadata or {}
        is_progress = bool(metadata.get("_progress"))
        source_message_id = metadata.get("message_id") or metadata.get("source_message_id")
        phase = str(metadata.get("_progress_phase") or "answer")
        reply_in_thread = self._resolve_reply_in_thread(metadata)
        show_thinking = bool(self.config.stream_card_show_thinking)
        if not source_message_id:
            if is_progress:
                logger.debug("Skip Feishu progress without source message_id")
                return True
            return False

        self._cleanup_stream_states()

        if is_progress:
            state = self._stream_states.get(source_message_id)
            if state is None:
                if not show_thinking and phase in {"thinking", "thinking_done"}:
                    return True

                initial_thinking = ""
                initial_answer = ""
                initial_collapsed = not show_thinking
                if phase == "thinking":
                    initial_thinking = (msg.content or "").strip()
                    if not self._has_specific_thinking_content(initial_thinking):
                        return True
                elif phase == "thinking_done":
                    return True
                else:
                    initial_answer = msg.content
                    initial_collapsed = True

                return (
                    await self._create_stream_state(
                        loop,
                        source_message_id,
                        receive_id_type,
                        msg.chat_id,
                        initial_thinking,
                        initial_answer,
                        initial_collapsed,
                        reply_in_thread,
                    )
                    is not None
                )

            min_update_seconds = max(0, int(self.config.stream_card_min_update_ms)) / 1000
            now = time.monotonic()
            state.updated_at = now
            if phase == "answer" and min_update_seconds > 0 and (now - state.last_update_at) < min_update_seconds:
                return True

            if not show_thinking and phase in {"thinking", "thinking_done"}:
                return True

            update_thinking = False
            update_answer = False
            if phase == "thinking":
                thinking_text = (msg.content or "").strip()
                incoming_lines = self._extract_specific_thinking_lines(thinking_text)
                if not incoming_lines:
                    return True

                merged_lines = self._extract_specific_thinking_lines(state.thinking_text)
                existing_lines = set(merged_lines)
                for line in incoming_lines:
                    if line not in existing_lines:
                        merged_lines.append(line)
                        existing_lines.add(line)

                merged_thinking = "\n".join(merged_lines)
                if state.thinking_collapsed or state.thinking_text != merged_thinking:
                    state.thinking_text = merged_thinking
                    state.thinking_collapsed = False
                    update_thinking = True
            elif phase == "thinking_done":
                if self._has_specific_thinking_content(state.thinking_text) and not state.thinking_collapsed:
                    state.thinking_collapsed = True
                    update_thinking = True
            else:
                answer_text = msg.content
                if (
                    show_thinking
                    and self._has_specific_thinking_content(state.thinking_text)
                    and not state.thinking_collapsed
                ):
                    state.thinking_collapsed = True
                    update_thinking = True
                if state.answer_text != answer_text:
                    state.answer_text = answer_text
                    update_answer = True

            updated = await self._update_stream_card(
                loop,
                state,
                update_thinking=update_thinking,
                update_answer=update_answer,
                allow_message_fallback=False,
            )
            if updated:
                now = time.monotonic()
                state.last_update_at = now
                state.updated_at = now
                self._remember_bot_message(state.bot_message_id, content=state.answer_text or state.thinking_text, chat_id=msg.chat_id, source_message_id=state.source_message_id)
                return True

            fallback_payload = self._build_streaming_initial_card_content(
                state.thinking_text,
                state.answer_text,
                state.thinking_collapsed,
            )
            old_bot_message_id = state.bot_message_id

            ok, fallback_message_id = await loop.run_in_executor(
                None,
                self._reply_message_detail_sync,
                state.source_message_id,
                "interactive",
                fallback_payload,
                state.reply_in_thread,
            )
            if not ok:
                ok, fallback_message_id = await loop.run_in_executor(
                    None,
                    self._send_message_detail_sync,
                    receive_id_type,
                    msg.chat_id,
                    "interactive",
                    fallback_payload,
                )
            if ok and fallback_message_id:
                now = time.monotonic()
                if old_bot_message_id and old_bot_message_id != fallback_message_id:
                    await loop.run_in_executor(
                        None,
                        self._delete_message_sync,
                        old_bot_message_id,
                    )
                state.bot_message_id = fallback_message_id
                state.updated_at = now
                state.last_update_at = now
                state.stream_uuid = uuid4().hex
                state.sequence = 0
                state.card_id = None
                await self._try_enable_cardkit_for_state(loop, state)
                self._remember_bot_message(fallback_message_id, content=state.answer_text or state.thinking_text, chat_id=msg.chat_id, source_message_id=state.source_message_id)
            return True

        state = self._stream_states.get(source_message_id)
        if state is None:
            return False

        update_thinking = False
        update_answer = False
        if (
            show_thinking
            and self._has_specific_thinking_content(state.thinking_text)
            and not state.thinking_collapsed
        ):
            state.thinking_collapsed = True
            update_thinking = True
        if state.answer_text != msg.content:
            state.answer_text = msg.content
            update_answer = True

        updated = await self._update_stream_card(
            loop,
            state,
            update_thinking=update_thinking,
            update_answer=update_answer,
            allow_message_fallback=True,
        )
        if updated:
            now = time.monotonic()
            state.last_update_at = now
            state.updated_at = now
            self._remember_bot_message(state.bot_message_id, content=state.answer_text, chat_id=msg.chat_id, source_message_id=state.source_message_id)
            return True

        fallback_payload = self._build_streaming_initial_card_content(
            state.thinking_text,
            state.answer_text,
            state.thinking_collapsed,
        )
        old_bot_message_id = state.bot_message_id

        ok, fallback_message_id = await loop.run_in_executor(
            None,
            self._reply_message_detail_sync,
            state.source_message_id,
            "interactive",
            fallback_payload,
            state.reply_in_thread,
        )
        if not ok:
            ok, fallback_message_id = await loop.run_in_executor(
                None,
                self._send_message_detail_sync,
                receive_id_type,
                msg.chat_id,
                "interactive",
                fallback_payload,
            )
        if ok and fallback_message_id:
            now = time.monotonic()
            if old_bot_message_id and old_bot_message_id != fallback_message_id:
                await loop.run_in_executor(
                    None,
                    self._delete_message_sync,
                    old_bot_message_id,
                )
            state.bot_message_id = fallback_message_id
            state.updated_at = now
            state.last_update_at = now
            state.stream_uuid = uuid4().hex
            state.sequence = 0
            state.card_id = None
            await self._try_enable_cardkit_for_state(loop, state)
            self._remember_bot_message(fallback_message_id, content=state.answer_text, chat_id=msg.chat_id, source_message_id=state.source_message_id)
        return True

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
                handled = False
                if self.config.stream_card_enabled:
                    handled = await self._send_streaming_content(msg, receive_id_type, loop)

                if not handled:
                    metadata = msg.metadata or {}
                    custom_interactive_content = metadata.get("interactive_content")
                    if isinstance(custom_interactive_content, dict):
                        fallback_payload = json.dumps(custom_interactive_content, ensure_ascii=False)
                    elif isinstance(custom_interactive_content, str) and custom_interactive_content.strip():
                        fallback_payload = custom_interactive_content
                    else:
                        fallback_payload = self._build_interactive_card_content(msg.content)

                    update_message_id = str(metadata.get("_update_message_id") or "").strip()
                    if update_message_id:
                        updated = await loop.run_in_executor(
                            None,
                            self._update_message_sync,
                            update_message_id,
                            "interactive",
                            fallback_payload,
                        )
                        if updated:
                            self._remember_bot_message(update_message_id, content=msg.content, chat_id=msg.chat_id, source_message_id=str(metadata.get("message_id") or "") or None)
                            return

                    replied = False
                    source_message_id = metadata.get("message_id")
                    reply_in_thread = self._resolve_reply_in_thread(metadata)
                    disable_reply_to_message = bool(metadata.get("_disable_reply_to_message"))
                    if self.config.reply_to_message and source_message_id and not disable_reply_to_message:
                        ok, replied_message_id = await loop.run_in_executor(
                            None,
                            self._reply_message_detail_sync,
                            source_message_id,
                            "interactive",
                            fallback_payload,
                            reply_in_thread,
                        )
                        replied = ok
                        if ok:
                            self._remember_bot_message(replied_message_id, content=msg.content, chat_id=msg.chat_id, source_message_id=source_message_id)

                    if not replied:
                        ok, sent_message_id = await loop.run_in_executor(
                            None,
                            self._send_message_detail_sync,
                            receive_id_type,
                            msg.chat_id,
                            "interactive",
                            fallback_payload,
                        )
                        if ok:
                            self._remember_bot_message(sent_message_id, content=msg.content, chat_id=msg.chat_id, source_message_id=str(source_message_id or "") or None)

        except Exception as e:
            logger.error("Error sending Feishu message: {}", e)

    def _schedule_background(self, coro: Any) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _on_p2p_chat_create_sync(self, data: Any) -> None:
        self._schedule_background(self._on_p2p_chat_create(data))

    async def _on_p2p_chat_create(self, data: Any) -> None:
        event = _safe_get(data, "event", None)
        chat_id = str(_safe_dig(event, "chat_id") or _safe_dig(event, "open_chat_id") or "")
        user_open_id = str(_safe_dig(event, "operator_id", "open_id") or _safe_dig(event, "user_id", "open_id") or "")
        if not chat_id or not user_open_id:
            return
        welcome_key = f"p2p:{chat_id}:{user_open_id}"
        if not self._mark_welcome_sent(welcome_key):
            return
        self._memory.upsert_feishu_user_profile(
            user_open_id,
            {
                "preferred_name": _safe_dig(event, "user_name") or "",
                "channel": "feishu",
                "first_source": "p2p_chat_create",
                "chat_id": chat_id,
            },
        )
        if self.config.onboarding_enabled:
            await self._handle_message(
                sender_id=user_open_id,
                chat_id=user_open_id,
                content="/setup",
                metadata={"source_event_type": "p2p_chat_create", "_bootstrap": True},
            )
            return
        await self.send(OutboundMessage(
            channel=self.name,
            chat_id=user_open_id,
            content="你好，我已经连接好了。可以先用 `/setup` 完成设置，或发 `/help` 查看命令。",
            metadata={"_disable_reply_to_message": True},
        ))

    def _on_message_read_sync(self, data: Any) -> None:
        self._schedule_background(self._on_message_read(data))

    async def _on_message_read(self, data: Any) -> None:
        event = _safe_get(data, "event", None)
        message_id = str(_safe_dig(event, "message_id") or _safe_dig(event, "open_message_id") or "")
        if not message_id:
            return
        logger.info("Feishu read event received for message {}", message_id)

    def _on_chat_member_added_sync(self, data: Any) -> None:
        self._schedule_background(self._on_chat_member_added(data))

    async def _on_chat_member_added(self, data: Any) -> None:
        event = _safe_get(data, "event", None)
        chat_id = str(_safe_dig(event, "chat_id") or "")
        user_open_id = str(_safe_dig(event, "user_id", "open_id") or _safe_dig(event, "operator_id", "open_id") or "")
        if not chat_id or not user_open_id:
            return
        if not self._group_welcome_allowed(chat_id):
            logger.info("Skip group welcome due to rate limit for {}", chat_id)
            return
        self._memory.upsert_feishu_chat_context(chat_id, {"last_joined_open_id": user_open_id, "channel": "feishu"})
        await self.send(OutboundMessage(
            channel=self.name,
            chat_id=chat_id,
            content="欢迎加入，可以 @ 我提问，常用命令有 `/help`、`/status`、`/session new`。",
            metadata={"_disable_reply_to_message": True, "_reply_in_thread": False},
        ))

    def _on_bitable_field_changed_sync(self, data: Any) -> None:
        self._schedule_background(self._on_bitable_field_changed(data))

    async def _on_bitable_field_changed(self, data: Any) -> None:
        event = _to_plain_data(_safe_get(data, "event", None))
        if not isinstance(event, dict):
            return
        await self._audit_sink.log_event(
            "feishu_bitable_field_changed",
            event_id=str(_safe_dig(data, "header", "event_id") or "") or None,
            chat_id=str(event.get("chat_id") or "") or None,
            payload={"event": event},
        )
        logger.info("Feishu bitable field change event: {}", _safe_json_dumps(event))
        await self._bitable_engine.handle_field_changed(event)

    def _on_bitable_record_changed_sync(self, data: Any) -> None:
        self._schedule_background(self._on_bitable_record_changed(data))

    async def _on_bitable_record_changed(self, data: Any) -> None:
        event = _to_plain_data(_safe_get(data, "event", None))
        if not isinstance(event, dict):
            return
        await self._audit_sink.log_event(
            "feishu_bitable_record_changed",
            event_id=str(_safe_dig(data, "header", "event_id") or "") or None,
            chat_id=str(event.get("chat_id") or "") or None,
            payload={"event": event},
        )
        logger.info("Feishu bitable record event: {}", _safe_json_dumps(event))
        await self._bitable_engine.handle_record_changed(event)

    def _on_card_action_sync(self, data: Any) -> None:
        """处理卡片动作回调的同步封装（由 WebSocket 线程触发）。"""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_card_action(data), self._loop)

    async def _on_card_action(self, data: Any) -> None:
        """解析并转发 Feishu 卡片动作回调到统一入站流程。"""
        try:
            event = _safe_get(data, "event", None)
            if event is None:
                logger.warning("Skip Feishu card action callback without event")
                return

            action = _safe_get(event, "action", None)
            if action is None:
                logger.warning("Skip Feishu card action callback without action payload")
                return

            context = _safe_get(event, "context", None)
            operator = _safe_get(event, "operator", None)

            content, action_key, action_tag = _build_card_action_content(action)

            sender_id = (
                _safe_dig(operator, "operator_id", "open_id")
                or _safe_dig(operator, "open_id")
                or _safe_dig(operator, "operator_id", "user_id")
                or _safe_dig(context, "open_id")
                or _safe_dig(context, "user_id", "open_id")
                or _safe_dig(context, "user_id")
                or "unknown"
            )
            sender_id = str(sender_id)

            open_message_id = (
                _safe_dig(context, "open_message_id")
                or _safe_dig(context, "message_id")
                or _safe_dig(event, "open_message_id")
                or _safe_dig(event, "message_id")
            )
            open_chat_id = (
                _safe_dig(context, "open_chat_id")
                or _safe_dig(event, "open_chat_id")
                or _safe_dig(event, "chat_id")
                or sender_id
            )

            event_type = (
                _safe_dig(data, "header", "event_type")
                or _safe_dig(event, "event_type")
                or "card.action.trigger"
            )
            await self._audit_sink.log_event(
                "feishu_card_action_received",
                event_id=str(_safe_dig(data, "header", "event_id") or "") or None,
                chat_id=str(open_chat_id),
                message_id=str(open_message_id or "") or None,
                payload={
                    "event_type": str(event_type),
                    "sender_id": sender_id,
                    "action_key": action_key,
                    "action_tag": action_tag,
                },
            )

            metadata = {
                "source_event_type": event_type,
                "msg_type": "card_action",
            }
            if action_tag:
                metadata["action_tag"] = action_tag
            action_name = _safe_get(action, "name", None)
            if isinstance(action_name, (str, int, float)) and str(action_name).strip():
                metadata["action_name"] = str(action_name).strip()
            if action_key:
                metadata["action_key"] = action_key
            if open_message_id:
                metadata["message_id"] = str(open_message_id)
                metadata["open_message_id"] = str(open_message_id)
            chat_type = _safe_dig(context, "chat_type")
            if chat_type:
                metadata["chat_type"] = str(chat_type)

            # 复用现有 _handle_message 做 allow-list 检查和统一入站分发
            await self._handle_message(
                sender_id=sender_id,
                chat_id=str(open_chat_id),
                content=content,
                metadata=metadata,
            )
        except Exception:
            logger.exception("Error processing Feishu card action callback")

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

            raw_content = message.content or ""

            try:
                content_json = json.loads(raw_content) if raw_content else {}
            except json.JSONDecodeError:
                content_json = {}

            text_for_activation = ""
            if msg_type == "text":
                text_for_activation = str(content_json.get("text", ""))
            elif msg_type == "post":
                text_for_activation = _extract_post_text(content_json)

            is_topic = self._is_topic_message(message)
            activation_policy = self._resolve_activation_policy(chat_type=chat_type, is_topic=is_topic)
            if activation_policy == "off":
                logger.debug("Drop Feishu inbound due to activation policy=off")
                return
            if activation_policy == "mention":
                mentioned = self._is_mentioned(
                    message,
                    content_json=content_json,
                    raw_content=raw_content,
                    text=text_for_activation,
                )
                allow_continuation = chat_type == "group" and self._is_continuation_command(text_for_activation)
                if (
                    not mentioned
                    and not allow_continuation
                    and not self._has_admin_prefix_bypass(sender_id=sender_id, content=text_for_activation)
                ):
                    logger.debug("Drop Feishu group message without activation mention")
                    return

            # Add reaction (optional)
            if self.config.react_enabled:
                await self._add_reaction(message_id, self.config.react_emoji)

            # Parse content
            content_parts = []
            media_paths = []

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
            if content:
                now = time.monotonic()
                fingerprint = f"{sender_id}:{reply_to}:{msg_type}:{content.strip()}"
                last_seen = self._recent_message_fingerprints.get(fingerprint)
                if last_seen is not None and (now - last_seen) < 3:
                    logger.debug("Skip duplicate inbound Feishu message by fingerprint")
                    return
                self._recent_message_fingerprints[fingerprint] = now

                while len(self._recent_message_fingerprints) > 1000:
                    self._recent_message_fingerprints.popitem(last=False)

                stale_before = now - 30
                while self._recent_message_fingerprints:
                    first_key = next(iter(self._recent_message_fingerprints))
                    if self._recent_message_fingerprints[first_key] >= stale_before:
                        break
                    self._recent_message_fingerprints.popitem(last=False)

            metadata = {
                "message_id": message_id,
                "chat_id": reply_to,
                "chat_type": chat_type,
                "msg_type": msg_type,
            }
            for key in ("root_id", "parent_id", "thread_id", "upper_message_id"):
                value = getattr(message, key, None)
                if value:
                    metadata[key] = value
            quoted_bot_summary = self._resolve_quoted_bot_summary(metadata)
            if quoted_bot_summary:
                metadata["quoted_bot_summary"] = quoted_bot_summary

            self._memory.upsert_feishu_user_profile(
                sender_id,
                {
                    "channel": "feishu",
                    "last_chat_id": reply_to,
                    "last_message_type": msg_type,
                },
            )
            if chat_type == "group":
                self._memory.upsert_feishu_chat_context(reply_to, {"chat_type": chat_type, "last_sender_open_id": sender_id})

            session_key = None
            thread_id = metadata.get("thread_id")
            if not thread_id and self._is_thread_context(metadata):
                thread_id = metadata.get("root_id")
            if chat_type == "group" and thread_id:
                session_key = f"{self.name}:{reply_to}:{thread_id}"

            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                media=media_paths,
                metadata=metadata,
                session_key=session_key,
            )

        except Exception as e:
            logger.error("Error processing Feishu message: {}", e)

#endregion
