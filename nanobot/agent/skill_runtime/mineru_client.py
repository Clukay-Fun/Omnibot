"""MinerU API client for document OCR and parsing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

import httpx

from nanobot.config.schema import MinerUConfig

SUPPORTED_DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".txt",
}


class MinerUClientError(RuntimeError):
    """Base MinerU client error."""


class MinerUTimeoutError(MinerUClientError):
    """Raised when polling exceeds configured timeout."""


class MinerUClient:
    """Async HTTP client for MinerU document processing."""

    def __init__(
        self,
        config: MinerUConfig,
        http_client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ):
        self.config = config
        self.http_client_factory = http_client_factory or httpx.AsyncClient

    async def submit_document(self, file_path: Path) -> str:
        """Submit a document and return MinerU task id."""
        self._validate_file(file_path)

        url = f"{self.config.api_base.rstrip('/')}/v1/tasks"
        headers = self._headers()

        with file_path.open("rb") as fp:
            files = {"file": (file_path.name, fp, self._guess_content_type(file_path))}
            data = await self._request("POST", url, headers=headers, files=files)

        task_id = data.get("task_id") or data.get("id")
        if not task_id:
            raise MinerUClientError("MinerU submit response missing task id")
        return str(task_id)

    async def get_task_result(self, task_id: str) -> dict[str, Any]:
        """Fetch current task state from MinerU."""
        if not task_id.strip():
            raise MinerUClientError("task_id is required")

        url = f"{self.config.api_base.rstrip('/')}/v1/tasks/{task_id}"
        return await self._request("GET", url, headers=self._headers())

    async def wait_for_result(self, task_id: str) -> dict[str, Any]:
        """Poll task result until success, failure, or timeout."""
        timeout_s = float(self.config.polling.timeout_seconds)
        interval_s = float(self.config.polling.interval_seconds)
        started = asyncio.get_running_loop().time()

        while True:
            payload = await self.get_task_result(task_id)
            status = str(payload.get("status", "")).lower()

            if status in {"succeeded", "success", "done", "completed"}:
                return payload
            if status in {"failed", "error", "cancelled", "canceled"}:
                reason = payload.get("error") or payload.get("message") or "unknown MinerU error"
                raise MinerUClientError(f"MinerU task {task_id} failed: {reason}")

            elapsed = asyncio.get_running_loop().time() - started
            if elapsed >= timeout_s:
                raise MinerUTimeoutError(
                    f"MinerU task {task_id} timed out after {timeout_s:.1f}s "
                    f"(last status: {status or 'unknown'})"
                )
            await asyncio.sleep(interval_s)

    async def submit_and_wait(self, file_path: Path) -> dict[str, Any]:
        """Submit a document then wait for completion."""
        task_id = await self.submit_document(file_path)
        return await self.wait_for_result(task_id)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        timeout = float(self.config.request.timeout_seconds)
        retries = int(self.config.request.max_retries)
        retry_delay = float(self.config.request.retry_delay_seconds)
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            try:
                async with self.http_client_factory(timeout=timeout) as client:
                    response = await client.request(method, url, **kwargs)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise MinerUClientError("MinerU response must be a JSON object")
                return data
            except (httpx.HTTPError, ValueError, MinerUClientError) as exc:
                last_error = exc
                if attempt >= retries:
                    break
                await asyncio.sleep(retry_delay)

        raise MinerUClientError(f"MinerU request failed after retries: {last_error}")

    def _validate_file(self, file_path: Path) -> None:
        if not file_path.exists() or not file_path.is_file():
            raise MinerUClientError(f"Document file not found: {file_path}")

        suffix = file_path.suffix.lower()
        if suffix not in SUPPORTED_DOCUMENT_EXTENSIONS:
            allowed = ", ".join(sorted(SUPPORTED_DOCUMENT_EXTENSIONS))
            raise MinerUClientError(
                f"Unsupported file format '{suffix or '<none>'}' for {file_path.name}. "
                f"Allowed formats: {allowed}"
            )

    @staticmethod
    def _guess_content_type(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return "application/pdf"
        if suffix in {".doc", ".docx"}:
            return "application/octet-stream"
        if suffix in {".png"}:
            return "image/png"
        if suffix in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if suffix in {".tif", ".tiff"}:
            return "image/tiff"
        if suffix in {".bmp"}:
            return "image/bmp"
        return "text/plain"
