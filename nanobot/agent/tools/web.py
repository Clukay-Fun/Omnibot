"""Web tools: web_search and web_fetch."""

import html
import json
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool

# Shared constants
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
MAX_REDIRECTS = 5  # Limit redirects to prevent DoS attacks


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate URL: must be http(s) with valid domain."""
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"
        return True, ""
    except Exception as e:
        return False, str(e)


class WebSearchTool(Tool):
    """Search the web using DuckDuckGo Instant Answer API."""

    name = "web_search"
    description = (
        "Search the web for current or external facts, research, and recent information. "
        "Use when the user is asking you to find or verify information beyond the current conversation. "
        "Do not use for greetings, identity questions, or conversational remarks that do not require lookup."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "description": "Results (1-10)", "minimum": 1, "maximum": 10}
        },
        "required": ["query"]
    }

    def __init__(self, api_key: str | None = None, max_results: int = 5, proxy: str | None = None):
        # Keep api_key for backward-compatible construction from existing config,
        # but DuckDuckGo Instant Answer does not require one.
        self._init_api_key = api_key
        self.max_results = max_results
        self.proxy = proxy

    async def execute(self, query: str, count: int | None = None, **kwargs: Any) -> str:
        try:
            n = min(max(count or self.max_results, 1), 10)
            logger.debug("WebSearch: {}", "proxy enabled" if self.proxy else "direct connection")
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await client.get(
                    "https://api.duckduckgo.com/",
                    params={
                        "q": query,
                        "format": "json",
                        "no_html": "1",
                        "no_redirect": "1",
                        "skip_disambig": "0",
                    },
                    headers={"Accept": "application/json"},
                    timeout=10.0
                )
                r.raise_for_status()

            results = _extract_duckduckgo_results(r.json(), query, n)
            if not results:
                return f"No results for: {query}"

            lines = [f"Results for: {query}\n"]
            for i, item in enumerate(results, 1):
                lines.append(f"{i}. {item.get('title', '')}\n   {item.get('url', '')}")
                if desc := item.get("description"):
                    lines.append(f"   {desc}")
            return "\n".join(lines)
        except httpx.ProxyError as e:
            logger.error("WebSearch proxy error: {}", e)
            return f"Proxy error: {e}"
        except Exception as e:
            logger.error("WebSearch error: {}", e)
            return f"Error: {e}"


def _extract_duckduckgo_results(payload: dict[str, Any], query: str, limit: int) -> list[dict[str, str]]:
    """Extract the most useful answer/topic results from DuckDuckGo Instant Answer."""
    results: list[dict[str, str]] = []

    def _append(title: str, url: str, description: str) -> None:
        if len(results) >= limit:
            return
        title = (title or query).strip()
        url = (url or "").strip()
        description = (description or "").strip()
        if not title and not description:
            return
        entry = {"title": title, "url": url, "description": description}
        if entry not in results:
            results.append(entry)

    abstract = (payload.get("AbstractText") or "").strip()
    abstract_url = (payload.get("AbstractURL") or "").strip()
    heading = (payload.get("Heading") or query).strip()
    answer = (payload.get("Answer") or "").strip()
    answer_type = (payload.get("AnswerType") or "").strip()
    definition = (payload.get("Definition") or "").strip()
    definition_url = (payload.get("DefinitionURL") or "").strip()

    if abstract:
        _append(heading or query, abstract_url, abstract)
    if answer:
        _append(answer_type or heading or query, abstract_url, answer)
    if definition:
        _append(f"{heading or query} definition", definition_url or abstract_url, definition)

    def _walk_related(items: list[dict[str, Any]]) -> None:
        for item in items:
            if len(results) >= limit:
                return
            topics = item.get("Topics")
            if isinstance(topics, list):
                _walk_related(topics)
                continue
            text = (item.get("Text") or "").strip()
            url = (item.get("FirstURL") or "").strip()
            if not text:
                continue
            title, _, desc = text.partition(" - ")
            _append(title or query, url, desc or text)

    related_topics = payload.get("RelatedTopics")
    if isinstance(related_topics, list):
        _walk_related(related_topics)

    results_section = payload.get("Results")
    if isinstance(results_section, list):
        for item in results_section:
            if len(results) >= limit:
                break
            text = (item.get("Text") or "").strip()
            url = (item.get("FirstURL") or "").strip()
            if not text:
                continue
            title, _, desc = text.partition(" - ")
            _append(title or query, url, desc or text)

    return results[:limit]


class WebFetchTool(Tool):
    """Fetch and extract content from a URL using Readability."""

    name = "web_fetch"
    description = (
        "Fetch a specific URL and extract readable content after you already know which page should be opened. "
        "Use for reading an article, page, or search result in detail. "
        "Do not use for general chat or broad discovery when no specific page needs to be opened."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "maxChars": {"type": "integer", "minimum": 100}
        },
        "required": ["url"]
    }

    def __init__(self, max_chars: int = 50000, proxy: str | None = None):
        self.max_chars = max_chars
        self.proxy = proxy

    async def execute(
        self,
        url: str,
        extract_mode: str = "markdown",
        max_chars_override: int | None = None,
        **kwargs: Any,
    ) -> str:
        from readability import Document

        # Keep backwards compatibility with tool-call schema aliases.
        if "extractMode" in kwargs:
            extract_mode = kwargs.pop("extractMode")
        if "maxChars" in kwargs:
            max_chars_override = kwargs.pop("maxChars")

        max_chars = max_chars_override or self.max_chars
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False)

        try:
            logger.debug("WebFetch: {}", "proxy enabled" if self.proxy else "direct connection")
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                timeout=30.0,
                proxy=self.proxy,
            ) as client:
                r = await client.get(url, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()

            ctype = r.headers.get("content-type", "")

            if "application/json" in ctype:
                text, extractor = json.dumps(r.json(), indent=2, ensure_ascii=False), "json"
            elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
                doc = Document(r.text)
                content = self._to_markdown(doc.summary()) if extract_mode == "markdown" else _strip_tags(doc.summary())
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                extractor = "readability"
            else:
                text, extractor = r.text, "raw"

            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]

            return json.dumps({"url": url, "finalUrl": str(r.url), "status": r.status_code,
                              "extractor": extractor, "truncated": truncated, "length": len(text), "text": text}, ensure_ascii=False)
        except httpx.ProxyError as e:
            logger.error("WebFetch proxy error for {}: {}", url, e)
            return json.dumps({"error": f"Proxy error: {e}", "url": url}, ensure_ascii=False)
        except Exception as e:
            logger.error("WebFetch error for {}: {}", url, e)
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)

    def _to_markdown(self, html: str) -> str:
        """Convert HTML to markdown."""
        # Convert links, headings, lists before stripping tags
        text = re.sub(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
                      lambda m: f'[{_strip_tags(m[2])}]({m[1]})', html, flags=re.I)
        text = re.sub(r'<h([1-6])[^>]*>([\s\S]*?)</h\1>',
                      lambda m: f'\n{"#" * int(m[1])} {_strip_tags(m[2])}\n', text, flags=re.I)
        text = re.sub(r'<li[^>]*>([\s\S]*?)</li>', lambda m: f'\n- {_strip_tags(m[1])}', text, flags=re.I)
        text = re.sub(r'</(p|div|section|article)>', '\n\n', text, flags=re.I)
        text = re.sub(r'<(br|hr)\s*/?>', '\n', text, flags=re.I)
        return _normalize(_strip_tags(text))
