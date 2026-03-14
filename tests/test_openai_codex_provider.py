from types import SimpleNamespace

import pytest

from nanobot.providers.openai_codex_provider import OpenAICodexProvider, _consume_sse


def _messages() -> list[dict]:
    return [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "查一下最新的比特币价格"},
    ]


def _tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Fallback DuckDuckGo web search.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_fetch",
                "description": "Fetch a URL.",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            },
        },
    ]


@pytest.mark.asyncio
async def test_codex_provider_prefers_hosted_web_search_before_local(monkeypatch) -> None:
    provider = OpenAICodexProvider()
    request_bodies: list[dict] = []

    monkeypatch.setattr(
        "nanobot.providers.openai_codex_provider.get_codex_token",
        lambda: SimpleNamespace(account_id="acct", access="token"),
    )

    async def _fake_request(_url, _headers, body, verify, progress_callback=None):
        request_bodies.append({"verify": verify, "body": body})
        return "ok", [], "stop"

    monkeypatch.setattr("nanobot.providers.openai_codex_provider._request_codex", _fake_request)

    response = await provider.chat(messages=_messages(), tools=_tools())

    assert response.content == "ok"
    assert len(request_bodies) == 1
    sent_tools = request_bodies[0]["body"]["tools"]
    assert sent_tools[0] == {"type": "web_search"}
    web_search_tool = next(tool for tool in sent_tools if tool.get("type") == "function" and tool.get("name") == "web_search")
    assert "Fallback DuckDuckGo web search" in web_search_tool["description"]
    assert any(tool.get("type") == "function" and tool.get("name") == "web_fetch" for tool in sent_tools)


@pytest.mark.asyncio
async def test_codex_provider_falls_back_to_local_web_search_when_hosted_request_fails(monkeypatch) -> None:
    provider = OpenAICodexProvider()
    request_bodies: list[dict] = []

    monkeypatch.setattr(
        "nanobot.providers.openai_codex_provider.get_codex_token",
        lambda: SimpleNamespace(account_id="acct", access="token"),
    )

    async def _fake_request(_url, _headers, body, verify, progress_callback=None):
        request_bodies.append({"verify": verify, "body": body})
        if len(request_bodies) == 1:
            raise RuntimeError("HTTP 400: hosted web search unavailable")
        return "fallback-ok", [], "stop"

    monkeypatch.setattr("nanobot.providers.openai_codex_provider._request_codex", _fake_request)

    response = await provider.chat(messages=_messages(), tools=_tools())

    assert response.content == "fallback-ok"
    assert len(request_bodies) == 2

    first_tools = request_bodies[0]["body"]["tools"]
    assert first_tools[0] == {"type": "web_search"}
    first_web_search = next(
        tool for tool in first_tools if tool.get("type") == "function" and tool.get("name") == "web_search"
    )
    assert "Fallback DuckDuckGo web search" in first_web_search["description"]

    second_tools = request_bodies[1]["body"]["tools"]
    assert any(tool.get("type") == "function" and tool.get("name") == "web_search" for tool in second_tools)
    assert all(tool.get("type") != "web_search" for tool in second_tools)


class _FakeSSEHttpxResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


@pytest.mark.asyncio
async def test_consume_sse_emits_progress_for_hosted_web_search() -> None:
    events = [
        'data: {"type":"response.output_item.added","item":{"id":"ws_1","type":"web_search_call","status":"in_progress"}}',
        "",
        'data: {"type":"response.web_search_call.searching","item_id":"ws_1"}',
        "",
        'data: {"type":"response.output_item.done","item":{"id":"ws_1","type":"web_search_call","status":"completed","action":{"type":"search","query":"北京市隆安（深圳）律师事务所 地址"}}}',
        "",
        'data: {"type":"response.output_item.added","item":{"id":"msg_1","type":"message","role":"assistant","status":"in_progress","content":[]}}',
        "",
        'data: {"type":"response.content_part.added","item_id":"msg_1","part":{"type":"output_text","text":""}}',
        "",
        'data: {"type":"response.output_text.delta","delta":"深圳市"}',
        "",
        'data: {"type":"response.completed","response":{"status":"completed"}}',
        "",
    ]
    progress: list[tuple[str, bool]] = []

    async def _progress_callback(content: str, *, tool_hint: bool = False) -> None:
        progress.append((content, tool_hint))

    content, tool_calls, finish_reason = await _consume_sse(
        _FakeSSEHttpxResponse(events),
        progress_callback=_progress_callback,
    )

    assert content == "深圳市"
    assert tool_calls == []
    assert finish_reason == "stop"
    assert progress == [
        ('web_search("联网查询")', True),
        ('web_search("北京市隆安（深圳）律师事务所 地址")', True),
    ]
