import httpx
import pytest

from nanobot.agent.tools.web import WebSearchTool, _extract_duckduckgo_results


def test_extract_duckduckgo_results_prefers_abstract_then_related_topics() -> None:
    payload = {
        "Heading": "OpenAI",
        "AbstractText": "OpenAI is an AI research and deployment company.",
        "AbstractURL": "https://openai.com/",
        "RelatedTopics": [
            {"Text": "OpenAI API - Developer platform", "FirstURL": "https://platform.openai.com/"},
            {
                "Name": "Products",
                "Topics": [
                    {"Text": "ChatGPT - Conversational AI product", "FirstURL": "https://chatgpt.com/"}
                ],
            },
        ],
    }

    results = _extract_duckduckgo_results(payload, "OpenAI", 3)

    assert results[0]["title"] == "OpenAI"
    assert "AI research" in results[0]["description"]
    assert results[1]["title"] == "OpenAI API"
    assert results[2]["title"] == "ChatGPT"


@pytest.mark.asyncio
async def test_web_search_tool_uses_duckduckgo_without_api_key(monkeypatch) -> None:
    captured = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "Heading": "Paris",
                "AbstractText": "Paris is the capital of France.",
                "AbstractURL": "https://example.com/paris",
            }

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url: str, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    tool = WebSearchTool(api_key=None, max_results=5)
    result = await tool.execute("capital of France", count=3)

    assert "Paris is the capital of France." in result
    assert captured["url"] == "https://api.duckduckgo.com/"
    assert captured["kwargs"]["params"]["q"] == "capital of France"
    assert captured["kwargs"]["params"]["format"] == "json"
