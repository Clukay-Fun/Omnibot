from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

from nanobot.agent.skills import SkillsLoader

SKILL_DIR = Path("nanobot/skills/http-api").resolve()
SCRIPT_DIR = SKILL_DIR / "scripts"
QUICK_VALIDATE_PATH = Path("nanobot/skills/skill-creator/scripts/quick_validate.py").resolve()


def _load_script_module(alias: str, filename: str):
    spec = importlib.util.spec_from_file_location(alias, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


http_mod = _load_script_module("http_api_http", "http.py")

quick_validate_spec = importlib.util.spec_from_file_location("http_api_quick_validate", QUICK_VALIDATE_PATH)
quick_validate = importlib.util.module_from_spec(quick_validate_spec)
assert quick_validate_spec and quick_validate_spec.loader
sys.modules["http_api_quick_validate"] = quick_validate
quick_validate_spec.loader.exec_module(quick_validate)


class _FakeResponse:
    def __init__(self, *, status: int, body: bytes, headers: dict[str, str], url: str):
        self.status = status
        self._body = body
        self.headers = headers
        self._url = url

    def read(self) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        return None


class _FakeOpener:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.requests = []

    def open(self, request, timeout=0):  # noqa: ANN001
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


def test_http_api_skill_is_valid_and_discoverable(tmp_path: Path) -> None:
    valid, message = quick_validate.validate_skill(SKILL_DIR)
    assert valid, message

    skills = SkillsLoader(tmp_path).list_skills(filter_unavailable=False)
    assert any(skill["name"] == "http-api" for skill in skills)

    skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "{baseDir}/scripts/http.sh request" in skill_md
    assert "Feishu 场景优先使用 `feishu-workspace`" in skill_md


def test_http_api_reference_has_expected_sections() -> None:
    ref = (SKILL_DIR / "references" / "http.md").read_text(encoding="utf-8")

    for heading in (
        "## 环境前提",
        "## 可用命令列表",
        "## 常见场景示例",
        "## 已知限制",
    ):
        assert heading in ref

    assert "site:open.feishu.cn" in ref


def test_http_request_once_parses_json_and_expands_env(monkeypatch) -> None:
    monkeypatch.setenv("TEST_TOKEN", "secret-123")

    headers = http_mod._parse_headers(["Authorization: Bearer ${TEST_TOKEN}"], None)
    opener = _FakeOpener(
        response=_FakeResponse(
            status=200,
            body=b'{"ok":true,"items":[1,2]}',
            headers={
                "Content-Type": "application/json",
                "x-request-id": "req-123",
            },
            url="https://api.example.com/items?page=2&tag=a&tag=b",
        )
    )

    result = http_mod.request_once(
        method="GET",
        url="https://api.example.com/items",
        headers=headers,
        query={"page": 2, "tag": ["a", "b"]},
        opener=opener,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == 200
    assert result["data"]["request_id"] == "req-123"
    assert result["data"]["body_json"]["items"] == [1, 2]

    request, timeout = opener.requests[0]
    assert request.full_url == "https://api.example.com/items?page=2&tag=a&tag=b"
    assert request.get_header("Authorization") == "Bearer secret-123"
    assert timeout == http_mod.DEFAULT_TIMEOUT


def test_http_request_once_blocks_private_targets() -> None:
    result = http_mod.request_once(method="GET", url="http://127.0.0.1/internal")

    assert result["ok"] is False
    assert result["error"]["kind"] == "validation_error"
    assert "URL validation failed" in result["error"]["message"]


def test_http_request_once_returns_structured_http_error() -> None:
    error = urllib.error.HTTPError(
        "https://api.example.com/items",
        403,
        "Forbidden",
        {"Content-Type": "application/json", "x-tt-logid": "log-1"},
        io.BytesIO(b'{"msg":"denied"}'),
    )
    opener = _FakeOpener(error=error)

    result = http_mod.request_once(
        method="POST",
        url="https://api.example.com/items",
        opener=opener,
    )

    assert result["ok"] is False
    assert result["error"]["kind"] == "http_error"
    assert result["error"]["status"] == 403
    assert result["error"]["request_id"] == "log-1"
    assert '{"msg":"denied"}' in result["error"]["body_text"]


def test_http_main_returns_validation_error_for_bad_query_json(capsys) -> None:
    exit_code = http_mod.main(
        [
            "request",
            "--method",
            "GET",
            "--url",
            "https://api.example.com/items",
            "--query-json",
            "[1,2,3]",
        ]
    )

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["error"]["kind"] == "validation_error"
