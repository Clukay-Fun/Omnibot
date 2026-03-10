"""Feishu webhook server with fast ack and async routing."""

from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

from loguru import logger

from nanobot.feishu.router import FeishuEnvelope, FeishuRouter
from nanobot.feishu.security import FeishuWebhookSecurity


class _WebhookHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], request_handler, service: "FeishuWebhookServer"):
        super().__init__(server_address, request_handler)
        self.service = service


class FeishuWebhookServer:
    """Run a lightweight webhook server and hand off events asynchronously."""

    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        security: FeishuWebhookSecurity,
        router: FeishuRouter,
    ):
        self.host = host
        self.port = port
        self.path = path
        self.security = security
        self.router = router
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: _WebhookHTTPServer | None = None
        self._thread: Thread | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                service._handle_post(self)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

        self._server = _WebhookHTTPServer((self.host, self.port), Handler, self)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.path != self.path:
            self._write_json(handler, 404, {"code": 404, "msg": "not found"})
            return

        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = handler.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8") if raw else "{}")
        except json.JSONDecodeError:
            self._write_json(handler, 400, {"code": 400, "msg": "invalid json"})
            return

        if not self.security.is_valid(payload):
            self._write_json(handler, 403, {"code": 403, "msg": "invalid token"})
            return

        if challenge := self.security.build_challenge_response(payload):
            self._write_json(handler, 200, challenge)
            return

        self._schedule(payload)
        self._write_json(handler, 200, {"code": 0})

    def _schedule(self, payload: dict[str, Any]) -> None:
        if self._loop is None:
            logger.warning("Feishu webhook received event before loop was ready")
            return
        envelope = FeishuEnvelope(source="webhook", payload=payload)
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self.router.route(envelope)))

    @staticmethod
    def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
