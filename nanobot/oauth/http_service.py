"""
描述: 轻便的内嵌 HTTP 会话回调服务器。
主要功能:
    - 为飞书 Oauth2.0 登陆拦截器提供 redirect_uri 挂载点，拉起本地端口承接 Auth Code 并分发给认证解析。
"""

from __future__ import annotations

import html
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from loguru import logger

from .feishu import FeishuOAuthService


class OAuthCallbackService:
    """
    用处: Oauth 客户端认证成功后的本地拦截器网关。

    功能:
        - 开启独立的 Http Server 线程监听指定的回调端口。
        - 拦截从三方平台转发回来的 code 等信息，送入后端逻辑提取并返回美化的 HTML 给用户看结果。
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        callback_path: str,
        feishu_service: FeishuOAuthService,
        success_title: str = "Feishu Authorization Completed",
        failure_title: str = "Feishu Authorization Failed",
    ):
        self.host = host
        self.port = port
        self.callback_path = callback_path if callback_path.startswith("/") else f"/{callback_path}"
        self.feishu_service = feishu_service
        self.success_title = success_title
        self.failure_title = failure_title
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return

        handler_class = self._build_handler()
        server = ThreadingHTTPServer((self.host, self.port), handler_class)
        thread = threading.Thread(target=server.serve_forever, daemon=True, name="oauth-callback-service")
        thread.start()

        self._server = server
        self._thread = thread
        self.port = int(server.server_port)
        logger.info("OAuth callback service listening on {}:{}{}", self.host, self.port, self.callback_path)

    def stop(self) -> None:
        if self._server is None:
            return
        try:
            self._server.shutdown()
            self._server.server_close()
        finally:
            self._server = None

        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        logger.info("OAuth callback service stopped")

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            server_version = "nanobot-oauth/1.0"

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                if parsed.path != parent.callback_path:
                    self._render(
                        404,
                        "Not Found",
                        "The requested callback path does not exist.",
                        success=False,
                    )
                    return

                query_raw = parse_qs(parsed.query, keep_blank_values=True)
                query: dict[str, str] = {key: values[0] if values else "" for key, values in query_raw.items()}
                try:
                    result = parent.feishu_service.handle_callback(query)
                except Exception as exc:
                    logger.exception("Unexpected OAuth callback failure: {}", exc)
                    self._render(
                        500,
                        parent.failure_title,
                        "Internal callback error.",
                        success=False,
                    )
                    return

                title = parent.success_title if result.success else parent.failure_title
                self._render(
                    result.status_code,
                    title,
                    result.message,
                    success=result.success,
                    details={"open_id": result.open_id} if result.open_id else None,
                )

            def do_POST(self) -> None:  # noqa: N802
                self._render(
                    405,
                    "Method Not Allowed",
                    "OAuth callback only accepts GET requests.",
                    success=False,
                )

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                logger.debug("OAuth callback {} - {}", self.client_address[0], format % args)

            def _render(
                self,
                status_code: int,
                title: str,
                message: str,
                *,
                success: bool,
                details: dict[str, str | None] | None = None,
            ) -> None:
                escaped_title = html.escape(title)
                escaped_message = html.escape(message)
                status_label = "Success" if success else "Failure"
                detail_html = ""
                if details:
                    lines = []
                    for key, value in details.items():
                        if value:
                            lines.append(f"<li><strong>{html.escape(str(key))}:</strong> {html.escape(str(value))}</li>")
                    if lines:
                        detail_html = "<ul>" + "".join(lines) + "</ul>"

                body = (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    f"<title>{escaped_title}</title>"
                    "<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:2rem;"
                    "line-height:1.5;color:#1f2937}"
                    ".card{max-width:680px;margin:0 auto;border:1px solid #e5e7eb;border-radius:12px;padding:1.25rem 1.5rem;"
                    "background:#fff}"
                    ".status{font-weight:700;margin-bottom:.5rem}"
                    ".ok{color:#047857}.fail{color:#b91c1c}"
                    "</style></head><body>"
                    "<div class='card'>"
                    f"<div class='status {'ok' if success else 'fail'}'>{status_label}</div>"
                    f"<h2>{escaped_title}</h2>"
                    f"<p>{escaped_message}</p>"
                    f"{detail_html}"
                    "<p>You can close this page and return to Feishu.</p>"
                    "</div></body></html>"
                )

                payload = body.encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return _Handler
