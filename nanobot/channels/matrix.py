"""Matrix (Element) 通讯频道的实现 — 负责支撑双向接收以及外联式纯文本带多媒体的数据内容交付与投递动作同步任务流。"""

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Any, TypeAlias

from loguru import logger

try:
    import nh3
    from mistune import create_markdown
    from nio import (
        AsyncClient, AsyncClientConfig, ContentRepositoryConfigError,
        DownloadError, InviteEvent, JoinError, MatrixRoom, MemoryDownloadResponse,
        RoomEncryptedMedia, RoomMessage, RoomMessageMedia, RoomMessageText,
        RoomSendError, RoomTypingError, SyncError, UploadError,
    )
    from nio.crypto.attachments import decrypt_attachment
    from nio.exceptions import EncryptionError
except ImportError as e:
    raise ImportError(
        "Matrix dependencies not installed. Run: pip install nanobot-ai[matrix]"
    ) from e

from nanobot.bus.events import OutboundMessage
from nanobot.channels.base import BaseChannel
from nanobot.config.loader import get_data_dir
from nanobot.utils.helpers import safe_filename

TYPING_NOTICE_TIMEOUT_MS = 30_000
# 必须小于 TYPING_NOTICE_TIMEOUT_MS 以防止提示状态在我们仍忙于解决请求中间过程的途中心跳过期。
TYPING_KEEPALIVE_INTERVAL_MS = 20_000
MATRIX_HTML_FORMAT = "org.matrix.custom.html"
_ATTACH_MARKER = "[attachment: {}]"
_ATTACH_TOO_LARGE = "[attachment: {} - too large]"
_ATTACH_FAILED = "[attachment: {} - download failed]"
_ATTACH_UPLOAD_FAILED = "[attachment: {} - upload failed]"
_DEFAULT_ATTACH_NAME = "attachment"
_MSGTYPE_MAP = {"m.image": "image", "m.audio": "audio", "m.video": "video", "m.file": "file"}

MATRIX_MEDIA_EVENT_FILTER = (RoomMessageMedia, RoomEncryptedMedia)
MatrixMediaEvent: TypeAlias = RoomMessageMedia | RoomEncryptedMedia

MATRIX_MARKDOWN = create_markdown(
    escape=True,
    plugins=["table", "strikethrough", "url", "superscript", "subscript"],
)

MATRIX_ALLOWED_HTML_TAGS = {
    "p", "a", "strong", "em", "del", "code", "pre", "blockquote",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "hr", "br", "table", "thead", "tbody", "tr", "th", "td",
    "caption", "sup", "sub", "img",
}
MATRIX_ALLOWED_HTML_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href"}, "code": {"class"}, "ol": {"start"},
    "img": {"src", "alt", "title", "width", "height"},
}
MATRIX_ALLOWED_URL_SCHEMES = {"https", "http", "matrix", "mailto", "mxc"}


# region [工具与辅助方法]

def _filter_matrix_html_attribute(tag: str, attr: str, value: str) -> str | None:
    """筛除过滤提取 Matrix 所支持认可且可以平稳使用的安全性限制较严格的一小部分特定的 HTML 属性集合列表值。"""
    if tag == "a" and attr == "href":
        return value if value.lower().startswith(("https://", "http://", "matrix:", "mailto:")) else None
    if tag == "img" and attr == "src":
        return value if value.lower().startswith("mxc://") else None
    if tag == "code" and attr == "class":
        classes = [c for c in value.split() if c.startswith("language-") and not c.startswith("language-_")]
        return " ".join(classes) if classes else None
    return value


MATRIX_HTML_CLEANER = nh3.Cleaner(
    tags=MATRIX_ALLOWED_HTML_TAGS,
    attributes=MATRIX_ALLOWED_HTML_ATTRIBUTES,
    attribute_filter=_filter_matrix_html_attribute,
    url_schemes=MATRIX_ALLOWED_URL_SCHEMES,
    strip_comments=True,
    link_rel="noopener noreferrer",
)


def _render_markdown_html(text: str) -> str | None:
    """借助 Markdown 原文本编译产生进行过安全保障级别过滤限制控制后的可显示版面的安全纯 HTML 节点产物; 对于未有特殊标签存在的文本内容我们通过返回 None 实现对这种平铺情况不做改动和影响。"""
    try:
        formatted = MATRIX_HTML_CLEANER.clean(MATRIX_MARKDOWN(text)).strip()
    except Exception:
        return None
    if not formatted:
        return None
    # 对普通的直白只有简单段落构成包裹如 <p>text</p> 的轻度格式要求内容，绕开采用额外 formatted_body 以免导致有效消息被无用庞大的负荷装填增加所埋没有必要处理开销。
    if formatted.startswith("<p>") and formatted.endswith("</p>"):
        inner = formatted[3:-4]
        if "<" not in inner and ">" not in inner:
            return None
    return formatted


def _build_matrix_text_content(text: str) -> dict[str, object]:
    """组织架构带有选择可配开关选项式的格式化了正文呈现支持 HTML 渲染排版在内的专供给 Matrix m.text 定制版使用的传递包裹负荷数据体系。"""
    content: dict[str, object] = {"msgtype": "m.text", "body": text, "m.mentions": {}}
    if html := _render_markdown_html(text):
        content["format"] = MATRIX_HTML_FORMAT
        content["formatted_body"] = html
    return content


class _NioLoguruHandler(logging.Handler):
    """引导截断汇聚标准基础的 matrix-nio Python 生态通用日志数据使其被重定向导流至 Loguru。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame, depth = frame.f_back, depth + 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _configure_nio_logging_bridge() -> None:
    """将 matrix-nio 的各种输出日志系统对接到 Loguru 体系上 (确保幂等形式的安全绑定执行手段)。"""
    nio_logger = logging.getLogger("nio")
    if not any(isinstance(h, _NioLoguruHandler) for h in nio_logger.handlers):
        nio_logger.handlers = [_NioLoguruHandler()]
        nio_logger.propagate = False


# endregion

# region [Matrix 频道核心类]

class MatrixChannel(BaseChannel):
    """通过采取使用长时间拉起持有关联等待反馈形态连接挂起操作来进行通信的底层驱动模式运行架构在之上的 Matrix (Element) 会话连接交流频道类封装。"""

    name = "matrix"

    def __init__(self, config: Any, bus, *, restrict_to_workspace: bool = False,
                 workspace: Path | None = None):
        super().__init__(config, bus)
        self.client: AsyncClient | None = None
        self._sync_task: asyncio.Task | None = None
        self._typing_tasks: dict[str, asyncio.Task] = {}
        self._restrict_to_workspace = restrict_to_workspace
        self._workspace = workspace.expanduser().resolve() if workspace else None
        self._server_upload_limit_bytes: int | None = None
        self._server_upload_limit_checked = False

    async def start(self) -> None:
        """开始调配启动初始化客户端 Matrix ，继而迈入一直尝试同步处理连接监听消息通讯事务的循环作业。"""
        self._running = True
        _configure_nio_logging_bridge()

        store_path = get_data_dir() / "matrix-store"
        store_path.mkdir(parents=True, exist_ok=True)

        self.client = AsyncClient(
            homeserver=self.config.homeserver, user=self.config.user_id,
            store_path=store_path,
            config=AsyncClientConfig(store_sync_tokens=True, encryption_enabled=self.config.e2ee_enabled),
        )
        self.client.user_id = self.config.user_id
        self.client.access_token = self.config.access_token
        self.client.device_id = self.config.device_id

        self._register_event_callbacks()
        self._register_response_callbacks()

        if not self.config.e2ee_enabled:
            logger.warning("Matrix E2EE disabled; encrypted rooms may be undecryptable.")

        if self.config.device_id:
            try:
                self.client.load_store()
            except Exception:
                logger.exception("Matrix store load failed; restart may replay recent messages.")
        else:
            logger.warning("Matrix device_id empty; restart may replay recent messages.")

        self._sync_task = asyncio.create_task(self._sync_loop())

    async def stop(self) -> None:
        """借助柔和优雅并且合规安全的正常停闭状态通知途径全面闭合并切断停止对于当前的所属实例频道全部连接响应活动动作过程运转机制。"""
        self._running = False
        for room_id in list(self._typing_tasks):
            await self._stop_typing_keepalive(room_id, clear_typing=False)
        if self.client:
            self.client.stop_sync_forever()
        if self._sync_task:
            try:
                await asyncio.wait_for(asyncio.shield(self._sync_task),
                                       timeout=self.config.sync_stop_grace_seconds)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._sync_task.cancel()
                try:
                    await self._sync_task
                except asyncio.CancelledError:
                    pass
        if self.client:
            await self.client.close()

    def _is_workspace_path_allowed(self, path: Path) -> bool:
        """检查限制确认相关的具体指定处理的访问操作目标地址空间到底在不在我们的可用正常授权覆盖范围内的情况存在现象（主要发生在一旦相应工作空间使用设防屏障参数配置时）。"""
        if not self._restrict_to_workspace or not self._workspace:
            return True
        try:
            path.resolve(strict=False).relative_to(self._workspace)
            return True
        except ValueError:
            return False

    def _collect_outbound_media_candidates(self, media: list[str]) -> list[Path]:
        """对于要进行向网路对面发布上传的待推送各种附属资源的存储物理地址的目录内容开展检查过滤，以防各种反复相同的操作指向及保证相对稳定的引用文件安全获取机制解析合并与汇总输出。"""
        seen: set[str] = set()
        candidates: list[Path] = []
        for raw in media:
            if not isinstance(raw, str) or not raw.strip():
                continue
            path = Path(raw.strip()).expanduser()
            try:
                key = str(path.resolve(strict=False))
            except OSError:
                key = str(path)
            if key not in seen:
                seen.add(key)
                candidates.append(path)
        return candidates

    @staticmethod
    def _build_outbound_attachment_content(
        *, filename: str, mime: str, size_bytes: int,
        mxc_url: str, encryption_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """拼装针对待完成提交传递上传环节的具体单个文档或各类照片图片文件乃至音影类记录所需的一套专门的匹配对应的格式结构化的包含信息数据的结构字典包裹包。"""
        prefix = mime.split("/")[0]
        msgtype = {"image": "m.image", "audio": "m.audio", "video": "m.video"}.get(prefix, "m.file")
        content: dict[str, Any] = {
            "msgtype": msgtype, "body": filename, "filename": filename,
            "info": {"mimetype": mime, "size": size_bytes}, "m.mentions": {},
        }
        if encryption_info:
            content["file"] = {**encryption_info, "url": mxc_url}
        else:
            content["url"] = mxc_url
        return content

    def _is_encrypted_room(self, room_id: str) -> bool:
        if not self.client:
            return False
        room = getattr(self.client, "rooms", {}).get(room_id)
        return bool(getattr(room, "encrypted", False))

    async def _send_room_content(self, room_id: str, content: dict[str, Any]) -> None:
        """发送夹带着端对端全加密 E2EE 加成防护设置条件的标准交互交流式基础 m.room.message 通讯通知反馈消息序列流。"""
        if not self.client:
            return
        kwargs: dict[str, Any] = {"room_id": room_id, "message_type": "m.room.message", "content": content}
        if self.config.e2ee_enabled:
            kwargs["ignore_unverified_devices"] = True
        await self.client.room_send(**kwargs)

    async def _resolve_server_upload_limit_bytes(self) -> int | None:
        """每个具体应用环境通道寿命的起止时期之中只会发生做过仅有那么一趟的网络服务器方规定所设想认可的当前数据上报的最大数据空间限制额度的访问确认反馈调拨验证操作。"""
        if self._server_upload_limit_checked:
            return self._server_upload_limit_bytes
        self._server_upload_limit_checked = True
        if not self.client:
            return None
        try:
            response = await self.client.content_repository_config()
        except Exception:
            return None
        upload_size = getattr(response, "upload_size", None)
        if isinstance(upload_size, int) and upload_size > 0:
            self._server_upload_limit_bytes = upload_size
            return upload_size
        return None

    async def _effective_media_limit_bytes(self) -> int:
        """选择最小限制范围条件作为实际运行安全值执行使用：即基于当前执行环境内自建限制控制和目标对向服务所要求的公开额度二者择取; 其中强制的零界定数值将会被理解做全局强制阻塞的闭锁含义。"""
        local_limit = max(int(self.config.max_media_bytes), 0)
        server_limit = await self._resolve_server_upload_limit_bytes()
        if server_limit is None:
            return local_limit
        return min(local_limit, server_limit) if local_limit else 0

    async def _upload_and_send_attachment(
        self, room_id: str, path: Path, limit_bytes: int,
        relates_to: dict[str, Any] | None = None,
    ) -> str | None:
        """接纳单笔物理地址的文件进入将其传导发送进至网络服务器之上随附发给相绑定对话终端目标的手段，如果在此步骤其中出现意外差错异常或违规，一经捕捉处理随后给出相关失败标签或什么都无（None）。"""
        if not self.client:
            return _ATTACH_UPLOAD_FAILED.format(path.name or _DEFAULT_ATTACH_NAME)

        resolved = path.expanduser().resolve(strict=False)
        filename = safe_filename(resolved.name) or _DEFAULT_ATTACH_NAME
        fail = _ATTACH_UPLOAD_FAILED.format(filename)

        if not resolved.is_file() or not self._is_workspace_path_allowed(resolved):
            return fail
        try:
            size_bytes = resolved.stat().st_size
        except OSError:
            return fail
        if limit_bytes <= 0 or size_bytes > limit_bytes:
            return _ATTACH_TOO_LARGE.format(filename)

        mime = mimetypes.guess_type(filename, strict=False)[0] or "application/octet-stream"
        try:
            with resolved.open("rb") as f:
                upload_result = await self.client.upload(
                    f, content_type=mime, filename=filename,
                    encrypt=self.config.e2ee_enabled and self._is_encrypted_room(room_id),
                    filesize=size_bytes,
                )
        except Exception:
            return fail

        upload_response = upload_result[0] if isinstance(upload_result, tuple) else upload_result
        encryption_info = upload_result[1] if isinstance(upload_result, tuple) and isinstance(upload_result[1], dict) else None
        if isinstance(upload_response, UploadError):
            return fail
        mxc_url = getattr(upload_response, "content_uri", None)
        if not isinstance(mxc_url, str) or not mxc_url.startswith("mxc://"):
            return fail

        content = self._build_outbound_attachment_content(
            filename=filename, mime=mime, size_bytes=size_bytes,
            mxc_url=mxc_url, encryption_info=encryption_info,
        )
        if relates_to:
            content["m.relates_to"] = relates_to
        try:
            await self._send_room_content(room_id, content)
        except Exception:
            return fail
        return None

    async def send(self, msg: OutboundMessage) -> None:
        """响应往服务端通道外部发送任何产出加工数据流内容结果指令的操作；与此同时清除释放之前一直在打字工作状态（并不做进展响应和回复结果以外的情景例外）的假模假样掩饰状态。"""
        if not self.client:
            return
        text = msg.content or ""
        candidates = self._collect_outbound_media_candidates(msg.media)
        relates_to = self._build_thread_relates_to(msg.metadata)
        is_progress = bool((msg.metadata or {}).get("_progress"))
        try:
            failures: list[str] = []
            if candidates:
                limit_bytes = await self._effective_media_limit_bytes()
                for path in candidates:
                    if fail := await self._upload_and_send_attachment(
                        msg.chat_id, path, limit_bytes, relates_to):
                        failures.append(fail)
            if failures:
                text = f"{text.rstrip()}\n{chr(10).join(failures)}" if text.strip() else "\n".join(failures)
            if text or not candidates:
                content = _build_matrix_text_content(text)
                if relates_to:
                    content["m.relates_to"] = relates_to
                await self._send_room_content(msg.chat_id, content)
        finally:
            if not is_progress:
                await self._stop_typing_keepalive(msg.chat_id, clear_typing=True)

    def _register_event_callbacks(self) -> None:
        self.client.add_event_callback(self._on_message, RoomMessageText)
        self.client.add_event_callback(self._on_media_message, MATRIX_MEDIA_EVENT_FILTER)
        self.client.add_event_callback(self._on_room_invite, InviteEvent)

    def _register_response_callbacks(self) -> None:
        self.client.add_response_callback(self._on_sync_error, SyncError)
        self.client.add_response_callback(self._on_join_error, JoinError)
        self.client.add_response_callback(self._on_send_error, RoomSendError)

    def _log_response_error(self, label: str, response: Any) -> None:
        """分化级数和分界类别来做异常提示信息通知归档机制的建立分级执行：将那些由认证等身份验证权限而波及诱发的报错问题标记至 ERROR, 对于任何其它的其余报错类型通通降格统一采用 WARNING 的方式应对打印记录和归档。"""
        code = getattr(response, "status_code", None)
        is_auth = code in {"M_UNKNOWN_TOKEN", "M_FORBIDDEN", "M_UNAUTHORIZED"}
        is_fatal = is_auth or getattr(response, "soft_logout", False)
        (logger.error if is_fatal else logger.warning)("Matrix {} failed: {}", label, response)

    async def _on_sync_error(self, response: SyncError) -> None:
        self._log_response_error("sync", response)

    async def _on_join_error(self, response: JoinError) -> None:
        self._log_response_error("join", response)

    async def _on_send_error(self, response: RoomSendError) -> None:
        self._log_response_error("send", response)

    async def _set_typing(self, room_id: str, typing: bool) -> None:
        """在合理限度范畴和能力尽可能控制内地针对被指明频道的目前书写回应回复状态发生变更多方广播同步。"""
        if not self.client:
            return
        try:
            response = await self.client.room_typing(room_id=room_id, typing_state=typing,
                                                     timeout=TYPING_NOTICE_TIMEOUT_MS)
            if isinstance(response, RoomTypingError):
                logger.debug("Matrix typing failed for {}: {}", room_id, response)
        except Exception:
            pass

    async def _start_typing_keepalive(self, room_id: str) -> None:
        """执行激活被纳入到使用要求规格里面所以才予以实现的每隔周期时间即刻开启打字更新标识展示工作维持不断联线在线通知活跃信号发送系统任务机制。"""
        await self._stop_typing_keepalive(room_id, clear_typing=False)
        await self._set_typing(room_id, True)
        if not self._running:
            return

        async def loop() -> None:
            try:
                while self._running:
                    await asyncio.sleep(TYPING_KEEPALIVE_INTERVAL_MS / 1000)
                    await self._set_typing(room_id, True)
            except asyncio.CancelledError:
                pass

        self._typing_tasks[room_id] = asyncio.create_task(loop())

    async def _stop_typing_keepalive(self, room_id: str, *, clear_typing: bool) -> None:
        if task := self._typing_tasks.pop(room_id, None):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if clear_typing:
            await self._set_typing(room_id, False)

    async def _sync_loop(self) -> None:
        while self._running:
            try:
                await self.client.sync_forever(timeout=30000, full_state=True)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(2)

    async def _on_room_invite(self, room: MatrixRoom, event: InviteEvent) -> None:
        allow_from = self.config.allow_from or []
        if not allow_from or event.sender in allow_from:
            await self.client.join(room.room_id)

    def _is_direct_room(self, room: MatrixRoom) -> bool:
        count = getattr(room, "member_count", None)
        return isinstance(count, int) and count <= 2

    def _is_bot_mentioned(self, event: RoomMessage) -> bool:
        """审查核对来自消息负载中的特有被设定专有的 m.mentions (代表提到呼叫指点标记指示信息数据项) 有没有明确包括我们的当前响应执行本体存在事实。"""
        source = getattr(event, "source", None)
        if not isinstance(source, dict):
            return False
        mentions = (source.get("content") or {}).get("m.mentions")
        if not isinstance(mentions, dict):
            return False
        user_ids = mentions.get("user_ids")
        if isinstance(user_ids, list) and self.config.user_id in user_ids:
            return True
        return bool(self.config.allow_room_mentions and mentions.get("room") is True)

    def _should_process_message(self, room: MatrixRoom, event: RoomMessage) -> bool:
        """实行执行在群组管理操作机制下或特对针对私人通讯身份情况权限的约束确认检查验证。"""
        if not self.is_allowed(event.sender):
            return False
        if self._is_direct_room(room):
            return True
        policy = self.config.group_policy
        if policy == "open":
            return True
        if policy == "allowlist":
            return room.room_id in (self.config.group_allow_from or [])
        if policy == "mention":
            return self._is_bot_mentioned(event)
        return False

    def _media_dir(self) -> Path:
        d = get_data_dir() / "media" / "matrix"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _event_source_content(event: RoomMessage) -> dict[str, Any]:
        source = getattr(event, "source", None)
        if not isinstance(source, dict):
            return {}
        content = source.get("content")
        return content if isinstance(content, dict) else {}

    def _event_thread_root_id(self, event: RoomMessage) -> str | None:
        relates_to = self._event_source_content(event).get("m.relates_to")
        if not isinstance(relates_to, dict) or relates_to.get("rel_type") != "m.thread":
            return None
        root_id = relates_to.get("event_id")
        return root_id if isinstance(root_id, str) and root_id else None

    def _thread_metadata(self, event: RoomMessage) -> dict[str, str] | None:
        if not (root_id := self._event_thread_root_id(event)):
            return None
        meta: dict[str, str] = {"thread_root_event_id": root_id}
        if isinstance(reply_to := getattr(event, "event_id", None), str) and reply_to:
            meta["thread_reply_to_event_id"] = reply_to
        return meta

    @staticmethod
    def _build_thread_relates_to(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
        if not metadata:
            return None
        root_id = metadata.get("thread_root_event_id")
        if not isinstance(root_id, str) or not root_id:
            return None
        reply_to = metadata.get("thread_reply_to_event_id") or metadata.get("event_id")
        if not isinstance(reply_to, str) or not reply_to:
            return None
        return {"rel_type": "m.thread", "event_id": root_id,
                "m.in_reply_to": {"event_id": reply_to}, "is_falling_back": True}

    def _event_attachment_type(self, event: MatrixMediaEvent) -> str:
        msgtype = self._event_source_content(event).get("msgtype")
        return _MSGTYPE_MAP.get(msgtype, "file")

    @staticmethod
    def _is_encrypted_media_event(event: MatrixMediaEvent) -> bool:
        return (isinstance(getattr(event, "key", None), dict)
                and isinstance(getattr(event, "hashes", None), dict)
                and isinstance(getattr(event, "iv", None), str))

    def _event_declared_size_bytes(self, event: MatrixMediaEvent) -> int | None:
        info = self._event_source_content(event).get("info")
        size = info.get("size") if isinstance(info, dict) else None
        return size if isinstance(size, int) and size >= 0 else None

    def _event_mime(self, event: MatrixMediaEvent) -> str | None:
        info = self._event_source_content(event).get("info")
        if isinstance(info, dict) and isinstance(m := info.get("mimetype"), str) and m:
            return m
        m = getattr(event, "mimetype", None)
        return m if isinstance(m, str) and m else None

    def _event_filename(self, event: MatrixMediaEvent, attachment_type: str) -> str:
        body = getattr(event, "body", None)
        if isinstance(body, str) and body.strip():
            if candidate := safe_filename(Path(body).name):
                return candidate
        return _DEFAULT_ATTACH_NAME if attachment_type == "file" else attachment_type

    def _build_attachment_path(self, event: MatrixMediaEvent, attachment_type: str,
                               filename: str, mime: str | None) -> Path:
        safe_name = safe_filename(Path(filename).name) or _DEFAULT_ATTACH_NAME
        suffix = Path(safe_name).suffix
        if not suffix and mime:
            if guessed := mimetypes.guess_extension(mime, strict=False):
                safe_name, suffix = f"{safe_name}{guessed}", guessed
        stem = (Path(safe_name).stem or attachment_type)[:72]
        suffix = suffix[:16]
        event_id = safe_filename(str(getattr(event, "event_id", "") or "evt").lstrip("$"))
        event_prefix = (event_id[:24] or "evt").strip("_")
        return self._media_dir() / f"{event_prefix}_{stem}{suffix}"

    async def _download_media_bytes(self, mxc_url: str) -> bytes | None:
        if not self.client:
            return None
        response = await self.client.download(mxc=mxc_url)
        if isinstance(response, DownloadError):
            logger.warning("Matrix download failed for {}: {}", mxc_url, response)
            return None
        body = getattr(response, "body", None)
        if isinstance(body, (bytes, bytearray)):
            return bytes(body)
        if isinstance(response, MemoryDownloadResponse):
            return bytes(response.body)
        if isinstance(body, (str, Path)):
            path = Path(body)
            if path.is_file():
                try:
                    return path.read_bytes()
                except OSError:
                    return None
        return None

    def _decrypt_media_bytes(self, event: MatrixMediaEvent, ciphertext: bytes) -> bytes | None:
        key_obj, hashes, iv = getattr(event, "key", None), getattr(event, "hashes", None), getattr(event, "iv", None)
        key = key_obj.get("k") if isinstance(key_obj, dict) else None
        sha256 = hashes.get("sha256") if isinstance(hashes, dict) else None
        if not all(isinstance(v, str) for v in (key, sha256, iv)):
            return None
        try:
            return decrypt_attachment(ciphertext, key, sha256, iv)
        except (EncryptionError, ValueError, TypeError):
            logger.warning("Matrix decrypt failed for event {}", getattr(event, "event_id", ""))
            return None

    async def _fetch_media_attachment(
        self, room: MatrixRoom, event: MatrixMediaEvent,
    ) -> tuple[dict[str, Any] | None, str]:
        """下载取回并于本地妥善安排安置（如有加密则还要负责破译恢复读取工作）一份专门给 Matrix 做匹配支持应用的附件。"""
        atype = self._event_attachment_type(event)
        mime = self._event_mime(event)
        filename = self._event_filename(event, atype)
        mxc_url = getattr(event, "url", None)
        fail = _ATTACH_FAILED.format(filename)

        if not isinstance(mxc_url, str) or not mxc_url.startswith("mxc://"):
            return None, fail

        limit_bytes = await self._effective_media_limit_bytes()
        declared = self._event_declared_size_bytes(event)
        if declared is not None and declared > limit_bytes:
            return None, _ATTACH_TOO_LARGE.format(filename)

        downloaded = await self._download_media_bytes(mxc_url)
        if downloaded is None:
            return None, fail

        encrypted = self._is_encrypted_media_event(event)
        data = downloaded
        if encrypted:
            if (data := self._decrypt_media_bytes(event, downloaded)) is None:
                return None, fail

        if len(data) > limit_bytes:
            return None, _ATTACH_TOO_LARGE.format(filename)

        path = self._build_attachment_path(event, atype, filename, mime)
        try:
            path.write_bytes(data)
        except OSError:
            return None, fail

        attachment = {
            "type": atype, "mime": mime, "filename": filename,
            "event_id": str(getattr(event, "event_id", "") or ""),
            "encrypted": encrypted, "size_bytes": len(data),
            "path": str(path), "mxc_url": mxc_url,
        }
        return attachment, _ATTACH_MARKER.format(path)

    def _base_metadata(self, room: MatrixRoom, event: RoomMessage) -> dict[str, Any]:
        """用来将对于基础的文本和其它如媒介媒体类通信等各个环节下，都可以广泛通用通用的具有共同公共数据架构价值特点的关键性特征和性质的信息等都预早的先组合汇聚构成的一个公共使用共享包内容的基础。"""
        meta: dict[str, Any] = {"room": getattr(room, "display_name", room.room_id)}
        if isinstance(eid := getattr(event, "event_id", None), str) and eid:
            meta["event_id"] = eid
        if thread := self._thread_metadata(event):
            meta.update(thread)
        return meta

    async def _on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        if event.sender == self.config.user_id or not self._should_process_message(room, event):
            return
        await self._start_typing_keepalive(room.room_id)
        try:
            await self._handle_message(
                sender_id=event.sender, chat_id=room.room_id,
                content=event.body, metadata=self._base_metadata(room, event),
            )
        except Exception:
            await self._stop_typing_keepalive(room.room_id, clear_typing=True)
            raise

    async def _on_media_message(self, room: MatrixRoom, event: MatrixMediaEvent) -> None:
        if event.sender == self.config.user_id or not self._should_process_message(room, event):
            return
        attachment, marker = await self._fetch_media_attachment(room, event)
        parts: list[str] = []
        if isinstance(body := getattr(event, "body", None), str) and body.strip():
            parts.append(body.strip())
        parts.append(marker)

        await self._start_typing_keepalive(room.room_id)
        try:
            meta = self._base_metadata(room, event)
            if attachment:
                meta["attachments"] = [attachment]
            await self._handle_message(
                sender_id=event.sender, chat_id=room.room_id,
                content="\n".join(parts),
                media=[attachment["path"]] if attachment else [],
                metadata=meta,
            )
        except Exception:
            await self._stop_typing_keepalive(room.room_id, clear_typing=True)
            raise

# endregion
