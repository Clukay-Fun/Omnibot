"""Embedding-assisted ranking for skillspec candidates.

This router is designed as a standalone utility. Deterministic rules should run first,
then this router can be used to rank fallback candidates.
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from nanobot.agent.skill_runtime.spec_schema import SkillSpec
from nanobot.config.schema import ProviderConfig

_SILICONFLOW_EMBEDDING_BASE = "https://api.siliconflow.cn/v1"
_TOKEN_RE = re.compile(r"\w+")


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    value: list[float]


class _TTLVectorCache:
    def __init__(self, ttl_seconds: int, now_fn: Callable[[], float] = time.time):
        self._ttl_seconds = max(ttl_seconds, 0)
        self._now_fn = now_fn
        self._entries: dict[str, _CacheEntry] = {}

    def get(self, key: str) -> list[float] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at < self._now_fn():
            self._entries.pop(key, None)
            return None
        return entry.value

    def set(self, key: str, value: list[float]) -> None:
        if self._ttl_seconds == 0:
            return
        expires_at = self._now_fn() + self._ttl_seconds
        self._entries[key] = _CacheEntry(expires_at=expires_at, value=value)


class EmbeddingSkillRouter:
    """Rank skills with embeddings when available, lexical fallback otherwise."""

    def __init__(
        self,
        *,
        embedding_enabled: bool = False,
        embedding_top_k: int = 3,
        embedding_model: str = "",
        embedding_timeout_seconds: int = 10,
        embedding_cache_ttl_seconds: int = 600,
        provider_config: ProviderConfig | None = None,
        http_client_factory: Callable[..., httpx.Client] = httpx.Client,
        now_fn: Callable[[], float] = time.time,
    ):
        self.embedding_enabled = embedding_enabled
        self.embedding_top_k = max(1, embedding_top_k)
        self.embedding_model = embedding_model.strip()
        self.embedding_timeout_seconds = max(1, embedding_timeout_seconds)
        self.provider_config = provider_config or ProviderConfig()
        self._http_client_factory = http_client_factory
        self._query_cache = _TTLVectorCache(embedding_cache_ttl_seconds, now_fn=now_fn)
        self._index_cache = _TTLVectorCache(embedding_cache_ttl_seconds, now_fn=now_fn)

    def rank(self, query: str, specs: dict[str, SkillSpec]) -> list[tuple[str, float]]:
        normalized_query = query.strip()
        if not normalized_query or not specs:
            return []

        docs = {skill_id: self._build_index_text(skill_id, spec) for skill_id, spec in specs.items()}
        if self._embedding_ready():
            try:
                return self._rank_by_embeddings(normalized_query, docs)
            except Exception:
                pass
        return self._rank_by_lexical(normalized_query, docs)

    def _embedding_ready(self) -> bool:
        return bool(
            self.embedding_enabled
            and self.embedding_model
            and self.provider_config.api_key
        )

    def _rank_by_embeddings(self, query: str, docs: dict[str, str]) -> list[tuple[str, float]]:
        query_key = self._query_key(query)
        query_vector = self._query_cache.get(query_key)
        if query_vector is None:
            query_vector = self._embed_texts([query])[0]
            self._query_cache.set(query_key, query_vector)

        skill_ids = sorted(docs)
        vectors: dict[str, list[float]] = {}
        missing_ids: list[str] = []
        missing_texts: list[str] = []

        for skill_id in skill_ids:
            index_key = self._index_key(skill_id, docs[skill_id])
            cached = self._index_cache.get(index_key)
            if cached is None:
                missing_ids.append(skill_id)
                missing_texts.append(docs[skill_id])
            else:
                vectors[skill_id] = cached

        if missing_texts:
            embedded = self._embed_texts(missing_texts)
            for skill_id, vector in zip(missing_ids, embedded, strict=False):
                vectors[skill_id] = vector
                self._index_cache.set(self._index_key(skill_id, docs[skill_id]), vector)

        scored = [
            (skill_id, self._cosine_similarity(query_vector, vectors[skill_id]))
            for skill_id in skill_ids
        ]
        return self._top_k(scored)

    def _rank_by_lexical(self, query: str, docs: dict[str, str]) -> list[tuple[str, float]]:
        query_tokens = Counter(self._tokenize(query))
        scored = [
            (skill_id, self._token_overlap_score(query_tokens, Counter(self._tokenize(text))))
            for skill_id, text in docs.items()
        ]
        return self._top_k(scored)

    def _embed_texts(self, inputs: list[str]) -> list[list[float]]:
        payload = {
            "model": self.embedding_model,
            "input": inputs,
        }
        url = self._embeddings_url()
        headers = {"Authorization": f"Bearer {self.provider_config.api_key}"}
        if self.provider_config.extra_headers:
            headers.update(self.provider_config.extra_headers)

        with self._http_client_factory(timeout=self.embedding_timeout_seconds) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()

        body = response.json()
        raw_data = body.get("data")
        if not isinstance(raw_data, list) or len(raw_data) != len(inputs):
            raise ValueError("unexpected embedding response shape")

        indexed = sorted(raw_data, key=lambda item: int(item.get("index", 0)))
        vectors: list[list[float]] = []
        for item in indexed:
            embedding = item.get("embedding")
            if not isinstance(embedding, list):
                raise ValueError("missing embedding vector")
            vectors.append([float(v) for v in embedding])
        return vectors

    def _build_index_text(self, skill_id: str, spec: SkillSpec) -> str:
        meta = spec.meta.model_dump()
        parts = [skill_id]
        for key in ("description", "match_description"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())

        for key in ("examples", "example"):
            value = meta.get(key)
            parts.extend(self._flatten_examples(value))

        return "\n".join(parts)

    def _flatten_examples(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, list):
            results: list[str] = []
            for item in value:
                results.extend(self._flatten_examples(item))
            return results
        if isinstance(value, dict):
            results: list[str] = []
            for item in value.values():
                results.extend(self._flatten_examples(item))
            return results
        return []

    def _tokenize(self, text: str) -> list[str]:
        return [token.lower() for token in _TOKEN_RE.findall(text)]

    def _token_overlap_score(self, query: Counter[str], doc: Counter[str]) -> float:
        if not query or not doc:
            return 0.0
        dot = sum(query[token] * doc[token] for token in query if token in doc)
        if dot == 0:
            return 0.0
        query_norm = math.sqrt(sum(v * v for v in query.values()))
        doc_norm = math.sqrt(sum(v * v for v in doc.values()))
        return dot / (query_norm * doc_norm) if query_norm and doc_norm else 0.0

    def _cosine_similarity(self, left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right, strict=False))
        left_norm = math.sqrt(sum(v * v for v in left))
        right_norm = math.sqrt(sum(v * v for v in right))
        return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0

    def _top_k(self, scored: list[tuple[str, float]]) -> list[tuple[str, float]]:
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[: self.embedding_top_k]

    def _query_key(self, query: str) -> str:
        return f"{self.embedding_model}::q::{query}"

    def _index_key(self, skill_id: str, text: str) -> str:
        return f"{self.embedding_model}::i::{skill_id}::{text}"

    def _embeddings_url(self) -> str:
        base = (self.provider_config.api_base or _SILICONFLOW_EMBEDDING_BASE).rstrip("/")
        return f"{base}/embeddings"
