from __future__ import annotations

from typing import Any

from nanobot.agent.skill_runtime.embedding_router import EmbeddingSkillRouter
from nanobot.agent.skill_runtime.spec_schema import SkillSpec
from nanobot.config.schema import ProviderConfig, SkillSpecConfig


def _build_spec(skill_id: str, description: str) -> SkillSpec:
    return SkillSpec.model_validate(
        {
            "meta": {
                "id": skill_id,
                "version": "0.1",
                "description": description,
                "match_description": description,
                "examples": [f"how to use {skill_id}"],
            },
            "params": {},
            "action": {"kind": "tool"},
            "response": {},
            "error": {},
        }
    )


class _FakeResponse:
    def __init__(self, body: dict[str, Any]):
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


class _EmbeddingClient:
    def __init__(self, vectors: dict[str, list[float]], counter: dict[str, int], **_: Any):
        self._vectors = vectors
        self._counter = counter

    def __enter__(self) -> _EmbeddingClient:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
        assert url.endswith("/embeddings")
        assert headers["Authorization"].startswith("Bearer ")
        self._counter["post_calls"] += 1
        inputs = json["input"]
        return _FakeResponse(
            {
                "data": [
                    {"index": idx, "embedding": self._vectors[text]}
                    for idx, text in enumerate(inputs)
                ]
            }
        )


class _FailingClient:
    def __init__(self, **_: Any):
        pass

    def __enter__(self) -> _FailingClient:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
        raise RuntimeError("provider unavailable")


def test_embedding_router_ranks_with_provider_embeddings() -> None:
    specs = {
        "alpha": _build_spec("alpha", "about alpha"),
        "beta": _build_spec("beta", "about beta"),
    }
    vectors = {
        "pick alpha": [1.0, 0.0],
        "alpha\nabout alpha\nabout alpha\nhow to use alpha": [1.0, 0.0],
        "beta\nabout beta\nabout beta\nhow to use beta": [0.0, 1.0],
    }
    counter = {"post_calls": 0}

    router = EmbeddingSkillRouter(
        embedding_enabled=True,
        embedding_top_k=2,
        embedding_model="text-embedding-3-small",
        provider_config=ProviderConfig(api_key="test-key", api_base="https://example.test/v1"),
        http_client_factory=lambda **kwargs: _EmbeddingClient(vectors, counter, **kwargs),
    )

    ranked = router.rank("pick alpha", specs)

    assert ranked[0][0] == "alpha"
    assert ranked[0][1] > ranked[1][1]
    assert counter["post_calls"] == 2


def test_embedding_router_falls_back_to_lexical_on_provider_error() -> None:
    specs = {
        "deadline_overview": _build_spec("deadline_overview", "show upcoming legal deadlines"),
        "contract_search": _build_spec("contract_search", "search contracts by term"),
    }

    router = EmbeddingSkillRouter(
        embedding_enabled=True,
        embedding_top_k=2,
        embedding_model="text-embedding-3-small",
        provider_config=ProviderConfig(api_key="test-key", api_base="https://example.test/v1"),
        http_client_factory=_FailingClient,
    )

    ranked = router.rank("upcoming deadlines", specs)

    assert ranked[0][0] == "deadline_overview"
    assert ranked[0][1] > 0


def test_embedding_router_uses_query_and_index_cache() -> None:
    specs = {
        "alpha": _build_spec("alpha", "about alpha"),
        "beta": _build_spec("beta", "about beta"),
    }
    vectors = {
        "pick alpha": [1.0, 0.0],
        "alpha\nabout alpha\nabout alpha\nhow to use alpha": [1.0, 0.0],
        "beta\nabout beta\nabout beta\nhow to use beta": [0.0, 1.0],
    }
    counter = {"post_calls": 0}

    router = EmbeddingSkillRouter(
        embedding_enabled=True,
        embedding_top_k=2,
        embedding_model="text-embedding-3-small",
        provider_config=ProviderConfig(api_key="test-key", api_base="https://example.test/v1"),
        embedding_cache_ttl_seconds=300,
        http_client_factory=lambda **kwargs: _EmbeddingClient(vectors, counter, **kwargs),
    )

    first = router.rank("pick alpha", specs)
    second = router.rank("pick alpha", specs)

    assert first == second
    assert counter["post_calls"] == 2


def test_skillspec_config_defaults_include_route_hardening() -> None:
    cfg = SkillSpecConfig()

    assert cfg.embedding_min_score == 0.15
    assert cfg.route_log_enabled is False
    assert cfg.route_log_top_k == 3
