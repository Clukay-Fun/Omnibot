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
REFERENCE_FILES = {
    "bitable": SKILL_DIR / "references" / "bitable.md",
    "calendar": SKILL_DIR / "references" / "calendar.md",
    "docs": SKILL_DIR / "references" / "docs.md",
}


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
    assert "tenant_access_token" in skill_md
    assert "不要直接复用历史回答" in skill_md
    assert "{baseDir}/references/bitable.md" in skill_md
    assert "{baseDir}/references/calendar.md" in skill_md
    assert "{baseDir}/references/docs.md" in skill_md


def test_feishu_workspace_skill_md_is_action_focused() -> None:
    skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    for heading in (
        "## 一句话定位",
        "## 触发条件",
        "## 开始前必做检查",
        "## 可执行操作清单",
        "## 不要尝试的操作清单",
        "## 失败处理规则",
    ):
        assert heading in skill_md

    assert "```bash" not in skill_md
    assert "Typical Commands" not in skill_md
    assert "部分支持" not in skill_md
    assert "后续补齐" not in skill_md
    assert "路线图" not in skill_md


def test_feishu_workspace_references_share_uniform_structure() -> None:
    for path in REFERENCE_FILES.values():
        text = path.read_text(encoding="utf-8")
        for heading in (
            "## 最小权限 Scope",
            "## 可用命令列表",
            "## 常见场景示例",
            "## 已知限制",
        ):
            assert heading in text


def test_feishu_workspace_skill_actions_are_covered_by_references() -> None:
    skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    bitable_md = REFERENCE_FILES["bitable"].read_text(encoding="utf-8")
    calendar_md = REFERENCE_FILES["calendar"].read_text(encoding="utf-8")
    docs_md = REFERENCE_FILES["docs"].read_text(encoding="utf-8")

    assert "读取 bitable 的 app、table、view、field、record 当前状态" in skill_md
    assert "app get" in bitable_md
    assert "table list|get" in bitable_md
    assert "view list|get" in bitable_md
    assert "field list|get|create|update|delete" in bitable_md
    assert "record list|get|create|update|delete|batch_create|batch_update|batch_delete" in bitable_md

    assert "读取 calendar、event 当前状态" in skill_md
    assert "calendar list|get" in calendar_md
    assert "event list|get|create|update|delete" in calendar_md

    assert "读取 doc 文本、wiki 节点、drive 文件当前状态" in skill_md
    assert "云文档协作者、公开分享设置、公开密码" in skill_md
    assert "drive list|search|get|delete" in docs_md
    assert "doc create|read_text|append_text|create_blocks|trash" in docs_md
    assert "permission member list|auth|create|batch_create|update|delete|transfer_owner" in docs_md
    assert "permission public get|patch" in docs_md
    assert "permission public password create|update|delete" in docs_md
    assert "raw request" in docs_md
    assert "wiki space_list|space_get|node_list|node_get|node_create|node_delete" in docs_md


def test_feishu_workspace_skill_boundaries_are_not_contradicted_by_references() -> None:
    skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    bitable_md = REFERENCE_FILES["bitable"].read_text(encoding="utf-8")
    calendar_md = REFERENCE_FILES["calendar"].read_text(encoding="utf-8")
    docs_md = REFERENCE_FILES["docs"].read_text(encoding="utf-8")

    assert "不要承诺访问用户私有日历、私有云盘文件" in skill_md
    assert "不要假设能访问用户私人日历" in calendar_md
    assert "不要假设能访问用户私人文件" in docs_md

    assert "不要做容器级删除" in skill_md
    assert "不支持创建或删除整个 bitable app" in bitable_md
    assert "不支持创建或删除整个 table" in bitable_md
    assert "不支持创建或删除整个 calendar" in calendar_md
    assert "未覆盖的 Feishu 官方 endpoint 请改用 `raw request`" in docs_md

    assert "不要把这里当作通用 doc 富文本编辑器" in skill_md
    assert "不支持通用富文本 block 编辑" in docs_md


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


def test_docs_create_blocks_appends_flat_root_level_blocks(monkeypatch, capsys) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.url.params),
                "body": body,
            }
        )
        return httpx.Response(200, json={"code": 0, "data": {"children": [{"block_id": "blk-1"}], "client_token": "idem-1"}})

    monkeypatch.setattr(
        docs_mod.common,
        "FeishuAPI",
        lambda module_name, scopes: _TransportAPI(module_name, scopes, handler),
    )

    children_json = json.dumps(
        [
            {"block_type": 3, "heading1": {"elements": [{"text_run": {"content": "周报"}}]}},
            {"block_type": 2, "text": {"elements": [{"text_run": {"content": "本周概览"}}]}},
            {"block_type": 12, "bullet": {"elements": [{"text_run": {"content": "事项 A"}}]}},
        ],
        ensure_ascii=False,
    )
    exit_code = docs_mod.main(
        [
            "doc",
            "create_blocks",
            "--document-id",
            "doccn123",
            "--children-json",
            children_json,
            "--client-token",
            "idem-1",
            "--document-revision-id",
            "-1",
            "--user-id-type",
            "open_id",
        ]
    )

    assert exit_code == 0
    assert seen[0]["method"] == "POST"
    assert seen[0]["path"] == "/open-apis/docx/v1/documents/doccn123/blocks/doccn123/children"
    assert seen[0]["query"] == {
        "client_token": "idem-1",
        "document_revision_id": "-1",
        "user_id_type": "open_id",
    }
    assert len(seen[0]["body"]["children"]) == 3
    output = json.loads(capsys.readouterr().out)
    assert output["meta"]["resource"] == "doc"
    assert output["meta"]["action"] == "create_blocks"


def test_docs_create_blocks_rejects_nested_or_unsupported_blocks() -> None:
    with pytest.raises(docs_mod.common.SkillError) as nested_exc:
        docs_mod.main(
            [
                "doc",
                "create_blocks",
                "--document-id",
                "doccn123",
                "--children-json",
                json.dumps(
                    [
                        {
                            "block_type": 2,
                            "text": {"elements": [{"text_run": {"content": "bad"}}]},
                            "children": [{"block_type": 2}],
                        }
                    ]
                ),
            ]
        )
    assert "flat root-level blocks" in str(nested_exc.value)

    with pytest.raises(docs_mod.common.SkillError) as unsupported_exc:
        docs_mod.main(
            [
                "doc",
                "create_blocks",
                "--document-id",
                "doccn123",
                "--children-json",
                json.dumps([{"block_type": 27, "image": {"token": "img"}}]),
            ]
        )
    assert "only supports block_type" in str(unsupported_exc.value)


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


def test_docs_permission_member_create_builds_expected_request(monkeypatch, capsys) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        seen.append({"method": request.method, "path": request.url.path, "query": dict(request.url.params), "body": body})
        return httpx.Response(200, json={"code": 0, "data": {"member": {"member_id": "ou_123"}}})

    monkeypatch.setattr(
        docs_mod.common,
        "FeishuAPI",
        lambda module_name, scopes: _TransportAPI(module_name, scopes, handler),
    )

    exit_code = docs_mod.main(
        [
            "permission",
            "member",
            "create",
            "--token",
            "doccn123",
            "--doc-type",
            "docx",
            "--member-type",
            "openid",
            "--member-id",
            "ou_123",
            "--perm",
            "edit",
            "--collaborator-type",
            "user",
            "--need-notification",
        ]
    )

    assert exit_code == 0
    assert seen[0]["method"] == "POST"
    assert seen[0]["path"] == "/open-apis/drive/v1/permissions/doccn123/members"
    assert seen[0]["query"]["type"] == "docx"
    assert seen[0]["query"]["need_notification"] == "true"
    assert seen[0]["body"] == {
        "member_type": "openid",
        "member_id": "ou_123",
        "perm": "edit",
        "type": "user",
    }
    output = json.loads(capsys.readouterr().out)
    assert output["meta"]["resource"] == "permission.member"
    assert output["meta"]["action"] == "create"


def test_docs_permission_member_delete_sends_query_and_body_fields(monkeypatch, capsys) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        seen.append({"method": request.method, "path": request.url.path, "query": dict(request.url.params), "body": body})
        return httpx.Response(200, json={"code": 0, "data": {"deleted": True}})

    monkeypatch.setattr(
        docs_mod.common,
        "FeishuAPI",
        lambda module_name, scopes: _TransportAPI(module_name, scopes, handler),
    )

    exit_code = docs_mod.main(
        [
            "permission",
            "member",
            "delete",
            "--token",
            "doccn123",
            "--doc-type",
            "docx",
            "--member-id",
            "ou_123",
            "--member-type",
            "openid",
            "--perm-type",
            "container",
            "--collaborator-type",
            "user",
        ]
    )

    assert exit_code == 0
    assert seen[0]["method"] == "DELETE"
    assert seen[0]["path"] == "/open-apis/drive/v1/permissions/doccn123/members/ou_123"
    assert seen[0]["query"]["type"] == "docx"
    assert seen[0]["query"]["member_type"] == "openid"
    assert seen[0]["body"] == {"type": "user", "perm_type": "container"}
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True


def test_docs_permission_member_transfer_owner_uses_query_flags(monkeypatch, capsys) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        seen.append({"method": request.method, "path": request.url.path, "query": dict(request.url.params), "body": body})
        return httpx.Response(200, json={"code": 0, "data": {"transferred": True}})

    monkeypatch.setattr(
        docs_mod.common,
        "FeishuAPI",
        lambda module_name, scopes: _TransportAPI(module_name, scopes, handler),
    )

    exit_code = docs_mod.main(
        [
            "permission",
            "member",
            "transfer_owner",
            "--token",
            "doccn123",
            "--doc-type",
            "docx",
            "--member-type",
            "openid",
            "--member-id",
            "ou_456",
            "--keep-old-owner",
            "--old-owner-perm",
            "edit",
            "--no-need-notification",
        ]
    )

    assert exit_code == 0
    assert seen[0]["method"] == "POST"
    assert seen[0]["path"] == "/open-apis/drive/v1/permissions/doccn123/members/transfer_owner"
    assert seen[0]["query"]["type"] == "docx"
    assert seen[0]["query"]["remove_old_owner"] == "false"
    assert seen[0]["query"]["old_owner_perm"] == "edit"
    assert seen[0]["query"]["need_notification"] == "false"
    assert seen[0]["body"] == {"member_type": "openid", "member_id": "ou_456"}
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True


def test_docs_permission_public_patch_defaults_to_v2_and_parses_docx_url(monkeypatch, capsys) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        seen.append({"method": request.method, "path": request.url.path, "query": dict(request.url.params), "body": body})
        return httpx.Response(200, json={"code": 0, "data": {"permission_public": {"share_entity": "anyone"}}})

    monkeypatch.setattr(
        docs_mod.common,
        "FeishuAPI",
        lambda module_name, scopes: _TransportAPI(module_name, scopes, handler),
    )

    exit_code = docs_mod.main(
        [
            "permission",
            "public",
            "patch",
            "--token",
            "https://example.feishu.cn/docx/doccn123",
            "--external-access-entity",
            "open",
            "--share-entity",
            "anyone",
            "--link-share-entity",
            "tenant_readable",
        ]
    )

    assert exit_code == 0
    assert seen[0]["method"] == "PATCH"
    assert seen[0]["path"] == "/open-apis/drive/v2/permissions/doccn123/public"
    assert seen[0]["query"]["type"] == "docx"
    assert seen[0]["body"] == {
        "external_access_entity": "open",
        "share_entity": "anyone",
        "link_share_entity": "tenant_readable",
    }
    output = json.loads(capsys.readouterr().out)
    assert output["meta"]["resource"] == "permission.public"
    assert output["meta"]["action"] == "patch"


def test_docs_permission_public_password_update_uses_v1_endpoint(monkeypatch, capsys) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"method": request.method, "path": request.url.path, "query": dict(request.url.params)})
        return httpx.Response(200, json={"code": 0, "data": {"password": "new-secret"}})

    monkeypatch.setattr(
        docs_mod.common,
        "FeishuAPI",
        lambda module_name, scopes: _TransportAPI(module_name, scopes, handler),
    )

    exit_code = docs_mod.main(
        [
            "permission",
            "public",
            "password",
            "update",
            "--token",
            "doccn123",
            "--doc-type",
            "docx",
        ]
    )

    assert exit_code == 0
    assert seen[0]["method"] == "PUT"
    assert seen[0]["path"] == "/open-apis/drive/v1/permissions/doccn123/public/password"
    assert seen[0]["query"]["type"] == "docx"
    output = json.loads(capsys.readouterr().out)
    assert output["meta"]["action"] == "password_update"


def test_common_request_raw_uses_tenant_auth_and_query_merge() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.url.params),
                "auth": request.headers.get("Authorization"),
            }
        )
        return httpx.Response(
            200,
            json={"code": 0, "msg": "ok", "data": {"ping": True}},
            headers={"x-tt-logid": "req-1"},
        )

    api = _TransportAPI("docs", ["scope-a"], handler)
    payload = api.request_raw(
        "GET",
        "https://open.feishu.cn/open-apis/drive/v1/files?folder_token=fld_1",
        params={"page_size": 50},
    )

    assert seen[0]["method"] == "GET"
    assert seen[0]["path"] == "/open-apis/drive/v1/files"
    assert seen[0]["query"]["folder_token"] == "fld_1"
    assert seen[0]["query"]["page_size"] == "50"
    assert seen[0]["auth"] == "Bearer test-token"
    assert payload["request_id"] == "req-1"
    assert payload["feishu_ok"] is True
    assert payload["body_json"]["data"]["ping"] is True


def test_docs_raw_request_accepts_bearer_env_and_preserves_feishu_errors(monkeypatch, capsys) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.url.params),
                "auth": request.headers.get("Authorization"),
                "body": json.loads(request.content.decode("utf-8")) if request.content else None,
            }
        )
        return httpx.Response(
            400,
            json={"code": 1063001, "msg": "Invalid parameter"},
            headers={"x-request-id": "req-raw-1"},
        )

    monkeypatch.setenv("FEISHU_USER_ACCESS_TOKEN", "user-token-1")
    monkeypatch.setattr(
        docs_mod.common,
        "FeishuAPI",
        lambda module_name, scopes: _TransportAPI(module_name, scopes, handler),
    )

    exit_code = docs_mod.main(
        [
            "raw",
            "request",
            "--method",
            "POST",
            "--path",
            "/open-apis/drive/v1/permissions/doccn123/members",
            "--query-json",
            '{"type":"docx"}',
            "--data-json",
            '{"member_id":"ou_123","member_type":"openid","perm":"edit"}',
            "--auth-mode",
            "bearer",
            "--bearer-token-env",
            "FEISHU_USER_ACCESS_TOKEN",
        ]
    )

    assert exit_code == 0
    assert seen[0]["method"] == "POST"
    assert seen[0]["path"] == "/open-apis/drive/v1/permissions/doccn123/members"
    assert seen[0]["query"]["type"] == "docx"
    assert seen[0]["auth"] == "Bearer user-token-1"
    assert seen[0]["body"]["member_id"] == "ou_123"
    output = json.loads(capsys.readouterr().out)
    assert output["meta"]["resource"] == "raw"
    assert output["data"]["status"] == 400
    assert output["data"]["feishu_code"] == 1063001
    assert output["data"]["feishu_message"] == "Invalid parameter"
    assert output["data"]["feishu_ok"] is False


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
