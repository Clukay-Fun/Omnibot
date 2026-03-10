from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from nanobot.agent.skill_runtime.mineru_client import (
    MinerUClient,
    MinerUClientError,
    MinerUTimeoutError,
)
from nanobot.config.schema import MinerUConfig, MinerUPollingConfig, MinerURequestConfig


def _client_factory(transport: httpx.MockTransport):
    def _factory(**kwargs):
        return httpx.AsyncClient(transport=transport, **kwargs)

    return _factory


@pytest.mark.asyncio
async def test_submit_and_wait_success(tmp_path: Path) -> None:
    document = tmp_path / "invoice.pdf"
    document.write_bytes(b"fake-pdf")
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if request.method == "POST" and request.url.path == "/v1/tasks":
            return httpx.Response(200, json={"task_id": "task-1"})
        if request.method == "GET" and request.url.path == "/v1/tasks/task-1":
            poll_count += 1
            if poll_count < 2:
                return httpx.Response(200, json={"status": "processing"})
            return httpx.Response(200, json={"status": "succeeded", "result": {"text": "ok"}})
        return httpx.Response(404, json={"message": "not found"})

    config = MinerUConfig(
        enabled=True,
        api_base="https://mineru.example",
        api_key="k",
        request=MinerURequestConfig(timeout_seconds=1.0, max_retries=0, retry_delay_seconds=0.01),
        polling=MinerUPollingConfig(interval_seconds=0.01, timeout_seconds=1.0),
    )
    client = MinerUClient(config, http_client_factory=_client_factory(httpx.MockTransport(handler)))

    payload = await client.submit_and_wait(document)

    assert payload["status"] == "succeeded"
    assert payload["result"]["text"] == "ok"


@pytest.mark.asyncio
async def test_submit_rejects_unsupported_format(tmp_path: Path) -> None:
    document = tmp_path / "archive.zip"
    document.write_bytes(b"fake-zip")
    config = MinerUConfig(enabled=True, api_base="https://mineru.example")
    client = MinerUClient(config, http_client_factory=_client_factory(httpx.MockTransport(lambda _: httpx.Response(500))))

    with pytest.raises(MinerUClientError) as exc:
        await client.submit_document(document)

    assert "Unsupported file format" in str(exc.value)
    assert "archive.zip" in str(exc.value)


@pytest.mark.asyncio
async def test_wait_for_result_times_out() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/tasks/task-timeout":
            return httpx.Response(200, json={"status": "processing"})
        return httpx.Response(404, json={"message": "not found"})

    config = MinerUConfig(
        enabled=True,
        api_base="https://mineru.example",
        request=MinerURequestConfig(timeout_seconds=1.0, max_retries=0, retry_delay_seconds=0.0),
        polling=MinerUPollingConfig(interval_seconds=0.01, timeout_seconds=0.03),
    )
    client = MinerUClient(config, http_client_factory=_client_factory(httpx.MockTransport(handler)))

    with pytest.raises(MinerUTimeoutError) as exc:
        await client.wait_for_result("task-timeout")

    assert "timed out" in str(exc.value)
