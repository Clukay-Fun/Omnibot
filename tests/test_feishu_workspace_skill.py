from __future__ import annotations

import builtins
import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from nanobot.agent.skills import SkillsLoader

SKILL_DIR = Path("nanobot/skills/feishu-workspace").resolve()
SCRIPT_DIR = SKILL_DIR / "scripts"
QUICK_VALIDATE_PATH = Path("nanobot/skills/skill-creator/scripts/quick_validate.py").resolve()


def _load_script_module(alias: str, filename: str):
    spec = importlib.util.spec_from_file_location(alias, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


common = _load_script_module("feishu_workspace_common", "common.py")
bitable_mod = _load_script_module("feishu_workspace_bitable", "bitable.py")
calendar_mod = _load_script_module("feishu_workspace_calendar", "calendar.py")
docs_mod = _load_script_module("feishu_workspace_docs", "docs.py")

quick_validate_spec = importlib.util.spec_from_file_location("feishu_quick_validate", QUICK_VALIDATE_PATH)
quick_validate = importlib.util.module_from_spec(quick_validate_spec)
assert quick_validate_spec and quick_validate_spec.loader
sys.modules["feishu_quick_validate"] = quick_validate
quick_validate_spec.loader.exec_module(quick_validate)


class _TransportAPI(common.FeishuAPI):
    def __init__(self, module_name, scopes, handler):
        super().__init__(
            module_name,
            scopes,
            auth_config=common.AuthConfig(auth_source="test", token="test-token"),
            transport=httpx.MockTransport(handler),
        )


def test_feishu_workspace_skill_is_valid_and_discoverable(tmp_path: Path) -> None:
    valid, message = quick_validate.validate_skill(SKILL_DIR)
    assert valid, message

    skills = SkillsLoader(tmp_path).list_skills(filter_unavailable=False)
    assert any(skill["name"] == "feishu-workspace" for skill in skills)

    skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert 'bash "{baseDir}/scripts/bitable.sh"' in skill_md
    assert "tenant_access_token" in skill_md
    assert "do not answer from prior conversation memory" in skill_md
    assert "Always run a fresh list/get/read/check command" in skill_md


def test_common_resolve_auth_prefers_env_token_and_import_failure_is_non_fatal(monkeypatch) -> None:
    monkeypatch.setenv("FEISHU_TENANT_ACCESS_TOKEN", "env-token")
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    auth = common.resolve_auth_config()
    assert auth.auth_source == "env:tenant_access_token"
    assert auth.token == "env-token"

    def _raising_import(name, *args, **kwargs):
        if name.startswith("nanobot.config.loader"):
            raise ImportError("blocked")
        return original_import(name, *args, **kwargs)

    monkeypatch.delenv("FEISHU_TENANT_ACCESS_TOKEN", raising=False)
    original_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", _raising_import)
    assert common._load_nanobot_config_credentials() is None


def test_common_permission_denied_includes_expected_scopes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"code": 99991672, "msg": "permission denied"},
            headers={"x-tt-logid": "log-123"},
        )

    api = _TransportAPI("bitable", ["scope-a", "scope-b"], handler)
    with pytest.raises(common.SkillError) as exc_info:
        api.request("GET", "/open-apis/bitable/v1/apps/app123")

    error = exc_info.value
    assert error.kind == "permission_denied"
    assert error.code == "99991672"
    assert error.request_id == "log-123"
    assert error.details["expected_scopes"] == ["scope-a", "scope-b"]


def test_bitable_record_list_uses_defaults_and_url_normalization(monkeypatch, capsys) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path, "query": dict(request.url.params)})
        return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})

    monkeypatch.setattr(
        bitable_mod.common,
        "FeishuAPI",
        lambda module_name, scopes: _TransportAPI(module_name, scopes, handler),
    )

    exit_code = bitable_mod.main(
        [
            "record",
            "list",
            "--url",
            "https://example.feishu.cn/base/app123?table=tbl456&view=vew789",
            "--page-token",
            "next-1",
        ]
    )

    assert exit_code == 0
    assert seen[0]["path"] == "/open-apis/bitable/v1/apps/app123/tables/tbl456/records"
    assert seen[0]["query"]["page_size"] == "20"
    assert seen[0]["query"]["page_token"] == "next-1"
    assert seen[0]["query"]["view_id"] == "vew789"
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True


def test_calendar_event_create_builds_expected_request(monkeypatch, capsys) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        seen.append({"method": request.method, "path": request.url.path, "query": dict(request.url.params), "body": body})
        return httpx.Response(200, json={"code": 0, "data": {"event": {"event_id": "evt-1"}}})

    monkeypatch.setattr(
        calendar_mod.common,
        "FeishuAPI",
        lambda module_name, scopes: _TransportAPI(module_name, scopes, handler),
    )

    exit_code = calendar_mod.main(
        [
            "event",
            "create",
            "--calendar-id",
            "cal_123",
            "--idempotency-key",
            "idem-1",
            "--data-json",
            '{"summary":"Demo","start_time":"2026-03-11T10:00:00+08:00","end_time":"2026-03-11T11:00:00+08:00"}',
        ]
    )

    assert exit_code == 0
    assert seen[0]["method"] == "POST"
    assert seen[0]["path"] == "/open-apis/calendar/v4/calendars/cal_123/events"
    assert seen[0]["query"]["idempotency_key"] == "idem-1"
    assert seen[0]["body"]["summary"] == "Demo"
    assert seen[0]["body"]["start_time"] == {
        "timestamp": str(int(datetime.fromisoformat("2026-03-11T10:00:00+08:00").timestamp())),
        "timezone": "UTC",
    }
    assert seen[0]["body"]["end_time"] == {
        "timestamp": str(int(datetime.fromisoformat("2026-03-11T11:00:00+08:00").timestamp())),
        "timezone": "UTC",
    }
    output = json.loads(capsys.readouterr().out)
    assert output["data"]["event"]["event_id"] == "evt-1"


def test_calendar_event_list_normalizes_iso_query_times(monkeypatch, capsys) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path, "query": dict(request.url.params)})
        return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})

    monkeypatch.setattr(
        calendar_mod.common,
        "FeishuAPI",
        lambda module_name, scopes: _TransportAPI(module_name, scopes, handler),
    )

    exit_code = calendar_mod.main(
        [
            "event",
            "list",
            "--calendar-id",
            "cal_123",
            "--start-time",
            "2026-03-11T10:00:00+08:00",
            "--end-time",
            "2026-03-11T11:00:00+08:00",
        ]
    )

    assert exit_code == 0
    assert seen[0]["path"] == "/open-apis/calendar/v4/calendars/cal_123/events"
    assert seen[0]["query"]["start_time"] == str(int(datetime.fromisoformat("2026-03-11T10:00:00+08:00").timestamp()))
    assert seen[0]["query"]["end_time"] == str(int(datetime.fromisoformat("2026-03-11T11:00:00+08:00").timestamp()))
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True


def test_calendar_list_uses_feishu_minimum_page_size(monkeypatch, capsys) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path, "query": dict(request.url.params)})
        return httpx.Response(200, json={"code": 0, "data": {"calendar_list": [], "has_more": False}})

    monkeypatch.setattr(
        calendar_mod.common,
        "FeishuAPI",
        lambda module_name, scopes: _TransportAPI(module_name, scopes, handler),
    )

    exit_code = calendar_mod.main(["calendar", "list", "--page-size", "20"])

    assert exit_code == 0
    assert seen[0]["path"] == "/open-apis/calendar/v4/calendars"
    assert seen[0]["query"]["page_size"] == "50"
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True


def test_docs_read_text_uses_placeholders_for_non_text_blocks() -> None:
    blocks = [
        {"block_id": "b1", "block_type": 2, "text": {"elements": [{"text_run": {"content": "hello"}}]}},
        {"block_id": "b2", "block_type": 31, "table": {"rows": 2}},
        {"block_id": "b3", "block_type": 27, "image": {"token": "img"}},
        {"block_id": "b4", "block_type": 8, "code": {"elements": [{"text_run": {"content": "print(1)"}}]}},
    ]

    payload = docs_mod._read_text_from_blocks(blocks, max_chars=1000)

    assert "hello" in payload["text"]
    assert "[表格]" in payload["text"]
    assert "[图片]" in payload["text"]
    assert "print(1)" in payload["text"]
    assert payload["truncated"] is False


def test_docs_append_text_uses_root_block_and_text_children(monkeypatch, capsys) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {"block_id": "root-block", "block_type": 1},
                            {"block_id": "text-1", "block_type": 2, "text": {"elements": [{"text_run": {"content": "existing"}}]}},
                        ],
                        "has_more": False,
                    },
                },
            )
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        seen.append({"method": request.method, "path": request.url.path, "body": body})
        return httpx.Response(200, json={"code": 0, "data": {"children": [{"block_id": "new-1"}]}})

    monkeypatch.setattr(
        docs_mod.common,
        "FeishuAPI",
        lambda module_name, scopes: _TransportAPI(module_name, scopes, handler),
    )

    exit_code = docs_mod.main(
        [
            "doc",
            "append_text",
            "--document-id",
            "doccn123",
            "--text",
            "First paragraph\n\nSecond paragraph",
        ]
    )

    assert exit_code == 0
    assert seen[0]["method"] == "POST"
    assert seen[0]["path"] == "/open-apis/docx/v1/documents/doccn123/blocks/root-block/children"
    assert len(seen[0]["body"]["children"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True


def test_docs_trash_and_drive_delete_include_required_type_query(monkeypatch, capsys) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.url.params),
            }
        )
        return httpx.Response(200, json={"code": 0, "data": {"deleted": True}})

    monkeypatch.setattr(
        docs_mod.common,
        "FeishuAPI",
        lambda module_name, scopes: _TransportAPI(module_name, scopes, handler),
    )

    assert docs_mod.main(["doc", "trash", "--document-id", "doccn123"]) == 0
    assert docs_mod.main(["drive", "delete", "--file-token", "filecn123"]) == 0

    assert seen[0]["method"] == "DELETE"
    assert seen[0]["path"] == "/open-apis/drive/v1/files/doccn123"
    assert seen[0]["query"]["type"] == "docx"
    assert seen[1]["method"] == "DELETE"
    assert seen[1]["path"] == "/open-apis/drive/v1/files/filecn123"
    assert seen[1]["query"]["type"] == "file"

    output_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert output_lines


def test_shell_wrapper_fails_without_repo_venv(tmp_path: Path) -> None:
    copied_skill = tmp_path / "nanobot" / "skills" / "feishu-workspace"
    shutil.copytree(SKILL_DIR, copied_skill)
    wrapper = copied_skill / "scripts" / "bitable.sh"

    result = subprocess.run(
        ["bash", str(wrapper), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "project virtualenv" in payload["error"]["message"]
