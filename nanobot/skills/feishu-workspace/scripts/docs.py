#!/usr/bin/env python3
"""CLI for Feishu docs, wiki, and drive operations."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path = [entry for entry in sys.path if Path(entry or ".").resolve() != SCRIPT_DIR]
COMMON_SPEC = importlib.util.spec_from_file_location("feishu_workspace_skill_common", SCRIPT_DIR / "common.py")
assert COMMON_SPEC and COMMON_SPEC.loader
common = importlib.util.module_from_spec(COMMON_SPEC)
sys.modules.setdefault("feishu_workspace_skill_common", common)
COMMON_SPEC.loader.exec_module(common)


MODULE_NAME = "docs"
EXPECTED_SCOPES = [
    "查看和评论新版文档",
    "创建和编辑新版文档",
    "查看、编辑和管理知识库",
    "查看、评论、编辑和管理云空间中所有文件",
    "上传、下载文件到云空间",
]
TEXT_BLOCK_TYPE = 2
PERMISSION_DOC_TYPES = ("doc", "sheet", "file", "wiki", "bitable", "docx", "folder", "mindnote", "minutes", "slides")
PUBLIC_PERMISSION_API_VERSIONS = ("v1", "v2")


def _success(api: common.FeishuAPI, args: argparse.Namespace, payload: dict[str, Any], *, paging: dict[str, Any] | None = None) -> int:
    resource = getattr(args, "resource", None)
    if resource == "permission" and getattr(args, "permission_target", None):
        resource = f"permission.{args.permission_target}"
    action = getattr(args, "action", None) or "check"
    if action == "password" and getattr(args, "password_action", None):
        action = f"password_{args.password_action}"
    return common.output_success(
        payload,
        paging=paging,
        meta={
            "module": MODULE_NAME,
            "resource": resource,
            "action": action,
            "auth_source": api.auth_metadata()["auth_source"],
        },
    )


def _doc_type_from_identifier(value: str, explicit_doc_type: str | None = None) -> tuple[str, str]:
    if explicit_doc_type:
        return explicit_doc_type, common.normalize_docx_or_file_token(value)[1] if value.startswith("http") else value
    kind, token = common.normalize_docx_or_file_token(value)
    kind_to_doc_type = {
        "document_id": "docx",
        "file_token": "file",
        "folder_token": "file",
        "node_token": "wiki",
        "raw": None,
    }
    doc_type = kind_to_doc_type.get(kind)
    if not doc_type:
        raise common.SkillError("validation_error", "Raw drive metadata requests require --doc-type.")
    return doc_type, token


def _drive_meta(api: common.FeishuAPI, identifier: str, *, doc_type: str | None = None) -> dict[str, Any]:
    actual_doc_type, token = _doc_type_from_identifier(identifier, explicit_doc_type=doc_type)
    payload = api.request(
        "POST",
        "/open-apis/drive/v1/metas/batch_query",
        json_body={"request_docs": [{"doc_token": token, "doc_type": actual_doc_type}], "with_url": True},
    )
    metas = payload.get("data", {}).get("metas", [])
    return metas[0] if metas else {}


def _parse_object_json_arg(value: str | None, *, field_name: str) -> dict[str, Any]:
    parsed = common.parse_json_arg(value, field_name=field_name)
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise common.SkillError("validation_error", f"{field_name} must be a JSON object.")
    return parsed


def _parse_list_json_arg(value: str | None, *, field_name: str) -> list[Any] | None:
    parsed = common.parse_json_arg(value, field_name=field_name)
    if parsed is None:
        return None
    if not isinstance(parsed, list):
        raise common.SkillError("validation_error", f"{field_name} must be a JSON array.")
    return parsed


def _add_optional_bool_flag(
    parser: argparse.ArgumentParser,
    dest: str,
    *,
    positive_flag: str | None = None,
    negative_flag: str | None = None,
    positive_help: str | None = None,
    negative_help: str | None = None,
) -> None:
    option_name = dest.replace("_", "-")
    positive_flag = positive_flag or f"--{option_name}"
    negative_flag = negative_flag or f"--no-{option_name}"
    group = parser.add_mutually_exclusive_group()
    group.add_argument(positive_flag, dest=dest, action="store_true", help=positive_help)
    group.add_argument(negative_flag, dest=dest, action="store_false", help=negative_help)
    parser.set_defaults(**{dest: None})


def _permission_target(identifier: str, explicit_doc_type: str | None = None) -> tuple[str, str]:
    if identifier.startswith("http"):
        parsed = urlparse(identifier)
        host = parsed.netloc.lower()
        if "feishu.cn" not in host and "larksuite.com" not in host:
            raise common.SkillError("validation_error", f"Unsupported Feishu permission URL: {identifier}")
        path = parsed.path.strip("/")
        parts = [segment for segment in path.split("/") if segment]
        inferred: tuple[str, str] | None = None
        if len(parts) >= 2 and parts[0] == "docx":
            inferred = ("docx", parts[1])
        elif len(parts) >= 2 and parts[0] == "docs":
            inferred = ("doc", parts[1])
        elif len(parts) >= 2 and parts[0] == "wiki":
            inferred = ("wiki", parts[1])
        elif len(parts) >= 2 and parts[0] == "file":
            inferred = ("file", parts[1])
        elif len(parts) >= 3 and parts[0] == "drive" and parts[1] == "folder":
            inferred = ("folder", parts[2])
        elif len(parts) >= 2 and parts[0] == "base":
            inferred = ("bitable", parts[1])
        if not inferred:
            raise common.SkillError("validation_error", f"Unsupported Feishu permission URL: {identifier}")
        if explicit_doc_type and explicit_doc_type != inferred[0]:
            raise common.SkillError(
                "validation_error",
                f"--doc-type {explicit_doc_type} does not match URL-derived type {inferred[0]}.",
            )
        return inferred
    if not explicit_doc_type:
        raise common.SkillError("validation_error", "Permission operations on raw tokens require --doc-type.")
    return explicit_doc_type, identifier


def _permission_params(
    actual_doc_type: str,
    *,
    params_json: str | None = None,
    include_need_notification: bool = False,
    need_notification: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = _parse_object_json_arg(params_json, field_name="params_json")
    params["type"] = actual_doc_type
    if include_need_notification and need_notification is not None:
        params["need_notification"] = need_notification
    for key, value in (extra or {}).items():
        if value is not None:
            params[key] = value
    return params


def _member_body_from_args(args: argparse.Namespace) -> dict[str, Any]:
    body = _parse_object_json_arg(getattr(args, "data_json", None), field_name="data_json")
    for key in ("member_type", "member_id", "perm", "perm_type"):
        value = getattr(args, key, None)
        if value is not None:
            body[key] = value
    collaborator_type = getattr(args, "collaborator_type", None)
    if collaborator_type is not None:
        body["type"] = collaborator_type
    return body


def _require_keys(payload: dict[str, Any], keys: list[str], *, field_name: str) -> None:
    missing = [key for key in keys if payload.get(key) in (None, "")]
    if missing:
        raise common.SkillError("validation_error", f"{field_name} is missing required keys: {', '.join(missing)}.")


def _fetch_doc_blocks(api: common.FeishuAPI, document_id: str) -> list[dict[str, Any]]:
    page_token: str | None = None
    blocks: list[dict[str, Any]] = []
    while True:
        payload = api.request(
            "GET",
            f"/open-apis/docx/v1/documents/{document_id}/blocks",
            params={"page_size": 100, "page_token": page_token},
        )
        data = payload.get("data", {})
        blocks.extend(data.get("items", []))
        page_token = data.get("page_token")
        if not data.get("has_more") or not page_token:
            break
    return blocks


def _extract_text_elements(text_block: dict[str, Any]) -> str:
    parts: list[str] = []
    for element in text_block.get("elements", []) or []:
        if "text_run" in element:
            parts.append(element["text_run"].get("content", ""))
        elif "mention_user" in element:
            parts.append(f"@{element['mention_user'].get('user_id', 'user')}")
        elif "mention_doc" in element:
            parts.append("[文档链接]")
        elif "equation" in element:
            parts.append("[公式]")
        elif "file" in element:
            parts.append("[附件]")
        elif "inline_block" in element:
            parts.append("[内联块]")
        else:
            parts.append("[内联内容]")
    return "".join(parts).strip()


def _block_to_text_line(block: dict[str, Any]) -> str | None:
    if "text" in block:
        return _extract_text_elements(block["text"])
    for level, prefix in (
        ("heading1", "# "),
        ("heading2", "## "),
        ("heading3", "### "),
        ("heading4", "#### "),
        ("heading5", "##### "),
        ("heading6", "###### "),
        ("heading7", "####### "),
        ("heading8", "######## "),
        ("heading9", "######### "),
        ("bullet", "- "),
        ("ordered", "1. "),
        ("quote", "> "),
        ("todo", "- [ ] "),
        ("code", "```text\n"),
    ):
        if level in block:
            text = _extract_text_elements(block[level])
            if level == "code":
                return f"```text\n{text}\n```"
            return prefix + text
    placeholder_map = {
        "image": "[图片]",
        "table": "[表格]",
        "bitable": "[多维表格]",
        "sheet": "[电子表格]",
        "iframe": "[嵌入内容]",
        "file": "[文件]",
        "diagram": "[流程图]",
        "mindnote": "[思维笔记]",
        "board": "[白板]",
        "agenda": "[议程]",
        "link_preview": "[链接预览]",
    }
    for key, placeholder in placeholder_map.items():
        if key in block:
            return placeholder
    return None


def _read_text_from_blocks(blocks: list[dict[str, Any]], *, max_chars: int) -> dict[str, Any]:
    lines: list[str] = []
    for block in blocks:
        line = _block_to_text_line(block)
        if line:
            lines.append(line)
    text = "\n".join(lines).strip()
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return {
        "text": text,
        "truncated": truncated,
        "block_count": len(blocks),
        "text_preview": common.text_preview(text, max_chars=200),
    }


def _find_root_block_id(blocks: list[dict[str, Any]], document_id: str) -> str:
    for block in blocks:
        if block.get("block_id") and block.get("block_type") == 1:
            return block["block_id"]
    for block in blocks:
        if block.get("block_id"):
            return block["block_id"]
    return document_id


def _make_text_block(content: str) -> dict[str, Any]:
    return {
        "block_type": TEXT_BLOCK_TYPE,
        "text": {
            "elements": [
                {
                    "text_run": {
                        "content": content,
                    }
                }
            ]
        },
    }


def _split_paragraphs(text: str) -> list[str]:
    paragraphs = [segment.strip() for segment in text.replace("\r\n", "\n").split("\n\n")]
    return [paragraph for paragraph in paragraphs if paragraph]


def _check(api: common.FeishuAPI, args: argparse.Namespace) -> int:
    if args.document_id:
        document_id = common.normalize_doc_token(args.document_id, expected_kind="doc")
        payload = api.request("GET", f"/open-apis/docx/v1/documents/{document_id}")
        probe = {"mode": "target_read", "document_id": document_id, "summary": payload.get("data", {}).get("document", {})}
    elif args.file_token:
        probe = {"mode": "target_read", "metadata": _drive_meta(api, args.file_token, doc_type=args.doc_type)}
    elif args.node_token:
        node_token = common.normalize_doc_token(args.node_token, expected_kind="wiki")
        payload = api.request("GET", "/open-apis/wiki/v2/spaces/get_node", params={"token": node_token})
        probe = {"mode": "target_read", "node": payload.get("data", {}).get("node", payload.get("data"))}
    else:
        payload = api.request("GET", "/open-apis/wiki/v2/spaces", params={"page_size": 1})
        probe = {
            "mode": "module_read",
            "spaces": (payload.get("data", {}).get("items") or payload.get("data", {}).get("spaces") or [])[:1],
        }
    return common.output_success(
        {
            **api.auth_metadata(),
            "expected_scopes": EXPECTED_SCOPES,
            "probe": probe,
        },
        meta={"module": MODULE_NAME, "action": "check"},
    )


def _handle_drive(api: common.FeishuAPI, args: argparse.Namespace) -> int:
    if args.action == "list":
        payload = api.request(
            "GET",
            "/open-apis/drive/v1/files",
            params={
                "folder_token": args.folder_token and common.normalize_doc_token(args.folder_token, expected_kind="folder"),
                "page_size": common.parse_page_size(args.page_size),
                "page_token": args.page_token,
                "order_by": args.order_by,
                "direction": args.direction,
            },
        )
        return _success(api, args, payload.get("data", {}), paging=common.clean_paging(payload))
    if args.action == "search":
        payload = api.request(
            "GET",
            "/open-apis/drive/v1/files",
            params={
                "folder_token": args.folder_token and common.normalize_doc_token(args.folder_token, expected_kind="folder"),
                "page_size": common.parse_page_size(args.page_size),
                "page_token": args.page_token,
            },
        )
        items = payload.get("data", {}).get("files") or payload.get("data", {}).get("items") or []
        query = (args.title_contains or "").casefold()
        if query:
            items = [item for item in items if query in str(item.get("name") or item.get("title") or "").casefold()]
        return _success(
            api,
            args,
            {"items": items, "search_mode": "folder_scan", "title_contains": args.title_contains},
            paging=common.clean_paging(payload),
        )
    if args.action == "get":
        return common.output_success(
            _drive_meta(api, args.identifier, doc_type=args.doc_type),
            meta={"module": MODULE_NAME, "resource": "drive", "action": "get", "auth_source": api.auth_metadata()["auth_source"]},
        )
    if args.action == "delete":
        file_token = common.normalize_doc_token(args.file_token, expected_kind="file")
        payload = api.request(
            "DELETE",
            f"/open-apis/drive/v1/files/{file_token}",
            params={"type": "file"},
        )
        return _success(api, args, payload.get("data", {}))
    raise common.SkillError("validation_error", f"Unsupported drive action: {args.action}")


def _handle_doc(api: common.FeishuAPI, args: argparse.Namespace) -> int:
    if args.action == "create":
        body: dict[str, Any] = {"title": args.title}
        if args.folder_token:
            body["folder_token"] = common.normalize_doc_token(args.folder_token, expected_kind="folder")
        payload = api.request("POST", "/open-apis/docx/v1/documents", json_body=body)
        return _success(api, args, payload.get("data", {}))
    document_id = common.normalize_doc_token(args.document_id, expected_kind="doc")
    if args.action == "read_text":
        blocks = _fetch_doc_blocks(api, document_id)
        payload = _read_text_from_blocks(blocks, max_chars=args.max_chars or common.DEFAULT_DOC_MAX_CHARS)
        payload["document_id"] = document_id
        return common.output_success(
            payload,
            meta={"module": MODULE_NAME, "resource": "doc", "action": "read_text", "auth_source": api.auth_metadata()["auth_source"]},
        )
    if args.action == "append_text":
        text = common.load_text_arg(args.text, args.text_file)
        blocks = _fetch_doc_blocks(api, document_id)
        root_block_id = _find_root_block_id(blocks, document_id)
        paragraphs = _split_paragraphs(text)
        if not paragraphs:
            raise common.SkillError("validation_error", "append_text requires at least one non-empty paragraph.")
        payload = api.request(
            "POST",
            f"/open-apis/docx/v1/documents/{document_id}/blocks/{root_block_id}/children",
            json_body={"children": [_make_text_block(paragraph) for paragraph in paragraphs]},
        )
        return _success(api, args, payload.get("data", {}))
    if args.action == "trash":
        payload = api.request(
            "DELETE",
            f"/open-apis/drive/v1/files/{document_id}",
            params={"type": "docx"},
        )
        return _success(api, args, payload.get("data", {}))
    raise common.SkillError("validation_error", f"Unsupported doc action: {args.action}")


def _handle_wiki(api: common.FeishuAPI, args: argparse.Namespace) -> int:
    if args.action == "space_list":
        payload = api.request(
            "GET",
            "/open-apis/wiki/v2/spaces",
            params={"page_size": common.parse_page_size(args.page_size), "page_token": args.page_token},
        )
        return _success(api, args, payload.get("data", {}), paging=common.clean_paging(payload))
    if args.action == "space_get":
        payload = api.request("GET", f"/open-apis/wiki/v2/spaces/{args.space_id}")
        return _success(api, args, payload.get("data", {}))
    if args.action == "node_list":
        payload = api.request(
            "GET",
            f"/open-apis/wiki/v2/spaces/{args.space_id}/nodes",
            params={
                "page_size": common.parse_page_size(args.page_size),
                "page_token": args.page_token,
                "parent_node_token": args.parent_node_token,
            },
        )
        return _success(api, args, payload.get("data", {}), paging=common.clean_paging(payload))
    if args.action == "node_get":
        token = common.normalize_doc_token(args.node_token, expected_kind="wiki")
        payload = api.request("GET", "/open-apis/wiki/v2/spaces/get_node", params={"token": token, "obj_type": args.obj_type})
        return _success(api, args, payload.get("data", {}))
    if args.action == "node_create":
        body = common.parse_json_arg(args.data_json, field_name="data_json")
        if not isinstance(body, dict):
            raise common.SkillError("validation_error", "data_json must be a JSON object.")
        payload = api.request("POST", f"/open-apis/wiki/v2/spaces/{args.space_id}/nodes", json_body=body)
        return _success(api, args, payload.get("data", {}))
    if args.action == "node_delete":
        token = common.normalize_doc_token(args.node_token, expected_kind="wiki")
        node_payload = api.request("GET", "/open-apis/wiki/v2/spaces/get_node", params={"token": token, "obj_type": args.obj_type})
        node = node_payload.get("data", {}).get("node", node_payload.get("data", {}))
        obj_token = node.get("obj_token")
        if not obj_token:
            raise common.SkillError("api_error", "Wiki node response did not include obj_token for delete fallback.", status=502)
        payload = api.request("DELETE", f"/open-apis/drive/v1/files/{obj_token}")
        return _success(api, args, {"deleted_node_token": token, "deleted_obj_token": obj_token, "drive_result": payload.get("data", {})})
    raise common.SkillError("validation_error", f"Unsupported wiki action: {args.action}")


def _handle_permission_member(api: common.FeishuAPI, args: argparse.Namespace) -> int:
    actual_doc_type, token = _permission_target(args.identifier, explicit_doc_type=args.doc_type)
    path_prefix = f"/open-apis/drive/v1/permissions/{token}/members"
    if args.action == "list":
        payload = api.request(
            "GET",
            path_prefix,
            params=_permission_params(
                actual_doc_type,
                params_json=args.params_json,
                extra={"fields": args.fields, "perm_type": args.perm_type},
            ),
        )
        return _success(api, args, payload.get("data", {}), paging=common.clean_paging(payload))
    if args.action == "auth":
        payload = api.request(
            "GET",
            f"{path_prefix}/auth",
            params=_permission_params(actual_doc_type, params_json=args.params_json),
        )
        return _success(api, args, payload.get("data", {}))
    if args.action == "create":
        body = _member_body_from_args(args)
        _require_keys(body, ["member_type", "member_id", "perm"], field_name="data_json/body")
        payload = api.request(
            "POST",
            path_prefix,
            params=_permission_params(
                actual_doc_type,
                params_json=args.params_json,
                include_need_notification=True,
                need_notification=args.need_notification,
            ),
            json_body=body,
        )
        return _success(api, args, payload.get("data", {}))
    if args.action == "batch_create":
        body = _parse_object_json_arg(args.data_json, field_name="data_json")
        members = _parse_list_json_arg(args.members_json, field_name="members_json")
        if members is not None:
            body["members"] = members
        _require_keys(body, ["members"], field_name="data_json/body")
        payload = api.request(
            "POST",
            f"{path_prefix}/batch_create",
            params=_permission_params(
                actual_doc_type,
                params_json=args.params_json,
                include_need_notification=True,
                need_notification=args.need_notification,
            ),
            json_body=body,
        )
        return _success(api, args, payload.get("data", {}))
    if args.action == "update":
        body = _member_body_from_args(args)
        body_member_id = body.pop("member_id", None)
        member_id = args.member_id or body_member_id
        if args.member_id and body_member_id and body_member_id != args.member_id:
            raise common.SkillError("validation_error", "member_id in data_json does not match --member-id.")
        if not member_id:
            raise common.SkillError("validation_error", "update requires --member-id or member_id in data_json.")
        _require_keys(body, ["member_type", "perm"], field_name="data_json/body")
        payload = api.request(
            "PUT",
            f"{path_prefix}/{member_id}",
            params=_permission_params(
                actual_doc_type,
                params_json=args.params_json,
                include_need_notification=True,
                need_notification=args.need_notification,
            ),
            json_body=body,
        )
        return _success(api, args, payload.get("data", {}))
    if args.action == "delete":
        body = _parse_object_json_arg(args.data_json, field_name="data_json")
        if args.collaborator_type is not None:
            body["type"] = args.collaborator_type
        if args.perm_type is not None:
            body["perm_type"] = args.perm_type
        params = _permission_params(
            actual_doc_type,
            params_json=args.params_json,
            extra={"member_type": args.member_type},
        )
        if not params.get("member_type"):
            raise common.SkillError("validation_error", "delete requires --member-type or member_type in --params-json.")
        payload = api.request(
            "DELETE",
            f"{path_prefix}/{args.member_id}",
            params=params,
            json_body=body or None,
        )
        return _success(api, args, payload.get("data", {}))
    if args.action == "transfer_owner":
        body = _parse_object_json_arg(args.data_json, field_name="data_json")
        if args.member_type is not None:
            body["member_type"] = args.member_type
        if args.member_id is not None:
            body["member_id"] = args.member_id
        _require_keys(body, ["member_type", "member_id"], field_name="data_json/body")
        payload = api.request(
            "POST",
            f"{path_prefix}/transfer_owner",
            params=_permission_params(
                actual_doc_type,
                params_json=args.params_json,
                include_need_notification=True,
                need_notification=args.need_notification,
                extra={"remove_old_owner": args.remove_old_owner, "old_owner_perm": args.old_owner_perm},
            ),
            json_body=body,
        )
        return _success(api, args, payload.get("data", {}))
    raise common.SkillError("validation_error", f"Unsupported permission member action: {args.action}")


def _handle_permission_public(api: common.FeishuAPI, args: argparse.Namespace) -> int:
    actual_doc_type, token = _permission_target(args.identifier, explicit_doc_type=args.doc_type)
    if args.action == "get":
        path = f"/open-apis/drive/{args.api_version}/permissions/{token}/public"
        payload = api.request("GET", path, params=_permission_params(actual_doc_type, params_json=args.params_json))
        return _success(api, args, payload.get("data", {}))
    if args.action == "patch":
        path = f"/open-apis/drive/{args.api_version}/permissions/{token}/public"
        body = _parse_object_json_arg(args.data_json, field_name="data_json")
        for key in ("security_entity", "comment_entity", "share_entity", "link_share_entity"):
            value = getattr(args, key, None)
            if value is not None:
                body[key] = value
        if args.api_version == "v1":
            if args.external_access_entity is not None:
                raise common.SkillError(
                    "validation_error",
                    "v1 public patch does not accept --external-access-entity; use --external-access.",
                )
            if args.external_access is not None:
                body["external_access"] = args.external_access
            if args.invite_external is not None:
                body["invite_external"] = args.invite_external
        else:
            if args.external_access_entity is not None:
                body["external_access_entity"] = args.external_access_entity
            if args.external_access is not None or args.invite_external is not None:
                raise common.SkillError(
                    "validation_error",
                    "v2 public patch does not accept --external-access or --invite-external; use --external-access-entity.",
                )
        if not body:
            raise common.SkillError("validation_error", "public patch requires at least one field or --data-json.")
        payload = api.request("PATCH", path, params=_permission_params(actual_doc_type, params_json=args.params_json), json_body=body)
        return _success(api, args, payload.get("data", {}))
    if args.action == "password":
        password_action_to_method = {"create": "POST", "update": "PUT", "delete": "DELETE"}
        method = password_action_to_method[args.password_action]
        payload = api.request(
            method,
            f"/open-apis/drive/v1/permissions/{token}/public/password",
            params=_permission_params(actual_doc_type, params_json=args.params_json),
        )
        return _success(api, args, payload.get("data", {}))
    raise common.SkillError("validation_error", f"Unsupported permission public action: {args.action}")


def _handle_permission(api: common.FeishuAPI, args: argparse.Namespace) -> int:
    if args.permission_target == "member":
        return _handle_permission_member(api, args)
    if args.permission_target == "public":
        return _handle_permission_public(api, args)
    raise common.SkillError("validation_error", f"Unsupported permission target: {args.permission_target}")


def build_parser() -> argparse.ArgumentParser:
    parser = common.build_parser("docs.py", "Operate Feishu docs, wiki, and drive resources.")
    subparsers = parser.add_subparsers(dest="resource", required=True)

    check = subparsers.add_parser("check", help="Validate docs/wiki/drive access.")
    check.add_argument("--document-id")
    check.add_argument("--file-token")
    check.add_argument("--node-token")
    check.add_argument("--doc-type")

    drive = subparsers.add_parser("drive", help="Drive file operations.")
    drive_sub = drive.add_subparsers(dest="action", required=True)
    drv_list = drive_sub.add_parser("list")
    drv_list.add_argument("--folder-token")
    drv_list.add_argument("--page-size", type=int)
    drv_list.add_argument("--page-token")
    drv_list.add_argument("--order-by")
    drv_list.add_argument("--direction")
    drv_search = drive_sub.add_parser("search")
    drv_search.add_argument("--folder-token")
    drv_search.add_argument("--title-contains")
    drv_search.add_argument("--page-size", type=int)
    drv_search.add_argument("--page-token")
    drv_get = drive_sub.add_parser("get")
    drv_get.add_argument("--identifier", required=True)
    drv_get.add_argument("--doc-type")
    drv_delete = drive_sub.add_parser("delete")
    drv_delete.add_argument("--file-token", required=True)

    doc = subparsers.add_parser("doc", help="Document operations.")
    doc_sub = doc.add_subparsers(dest="action", required=True)
    doc_create = doc_sub.add_parser("create")
    doc_create.add_argument("--title", required=True)
    doc_create.add_argument("--folder-token")
    doc_read = doc_sub.add_parser("read_text")
    doc_read.add_argument("--document-id", required=True)
    doc_read.add_argument("--max-chars", type=int)
    doc_append = doc_sub.add_parser("append_text")
    doc_append.add_argument("--document-id", required=True)
    doc_append.add_argument("--text")
    doc_append.add_argument("--text-file")
    doc_trash = doc_sub.add_parser("trash")
    doc_trash.add_argument("--document-id", required=True)

    wiki = subparsers.add_parser("wiki", help="Wiki operations.")
    wiki_sub = wiki.add_subparsers(dest="action", required=True)
    wiki_space_list = wiki_sub.add_parser("space_list")
    wiki_space_list.add_argument("--page-size", type=int)
    wiki_space_list.add_argument("--page-token")
    wiki_space_get = wiki_sub.add_parser("space_get")
    wiki_space_get.add_argument("--space-id", required=True)
    wiki_node_list = wiki_sub.add_parser("node_list")
    wiki_node_list.add_argument("--space-id", required=True)
    wiki_node_list.add_argument("--parent-node-token")
    wiki_node_list.add_argument("--page-size", type=int)
    wiki_node_list.add_argument("--page-token")
    wiki_node_get = wiki_sub.add_parser("node_get")
    wiki_node_get.add_argument("--node-token", required=True)
    wiki_node_get.add_argument("--obj-type")
    wiki_node_create = wiki_sub.add_parser("node_create")
    wiki_node_create.add_argument("--space-id", required=True)
    wiki_node_create.add_argument("--data-json", required=True)
    wiki_node_delete = wiki_sub.add_parser("node_delete")
    wiki_node_delete.add_argument("--node-token", required=True)
    wiki_node_delete.add_argument("--obj-type")

    permission = subparsers.add_parser("permission", help="Drive permission operations.")
    permission_sub = permission.add_subparsers(dest="permission_target", required=True)

    member = permission_sub.add_parser("member", help="Collaborator permission operations.")
    member_sub = member.add_subparsers(dest="action", required=True)

    member_list = member_sub.add_parser("list")
    member_list.add_argument("--token", dest="identifier", required=True)
    member_list.add_argument("--doc-type", choices=PERMISSION_DOC_TYPES)
    member_list.add_argument("--fields")
    member_list.add_argument("--perm-type")
    member_list.add_argument("--params-json")

    member_auth = member_sub.add_parser("auth")
    member_auth.add_argument("--token", dest="identifier", required=True)
    member_auth.add_argument("--doc-type", choices=PERMISSION_DOC_TYPES)
    member_auth.add_argument("--params-json")

    member_create = member_sub.add_parser("create")
    member_create.add_argument("--token", dest="identifier", required=True)
    member_create.add_argument("--doc-type", choices=PERMISSION_DOC_TYPES)
    member_create.add_argument("--member-type")
    member_create.add_argument("--member-id")
    member_create.add_argument("--perm")
    member_create.add_argument("--perm-type")
    member_create.add_argument("--collaborator-type")
    _add_optional_bool_flag(
        member_create,
        "need_notification",
        positive_help="Notify the target after granting access when supported.",
        negative_help="Do not notify the target after granting access when supported.",
    )
    member_create.add_argument("--params-json")
    member_create.add_argument("--data-json")

    member_batch_create = member_sub.add_parser("batch_create")
    member_batch_create.add_argument("--token", dest="identifier", required=True)
    member_batch_create.add_argument("--doc-type", choices=PERMISSION_DOC_TYPES)
    _add_optional_bool_flag(
        member_batch_create,
        "need_notification",
        positive_help="Notify targets after granting access when supported.",
        negative_help="Do not notify targets after granting access when supported.",
    )
    member_batch_create.add_argument("--params-json")
    member_batch_create.add_argument("--data-json")
    member_batch_create.add_argument("--members-json")

    member_update = member_sub.add_parser("update")
    member_update.add_argument("--token", dest="identifier", required=True)
    member_update.add_argument("--doc-type", choices=PERMISSION_DOC_TYPES)
    member_update.add_argument("--member-id")
    member_update.add_argument("--member-type")
    member_update.add_argument("--perm")
    member_update.add_argument("--perm-type")
    member_update.add_argument("--collaborator-type")
    _add_optional_bool_flag(
        member_update,
        "need_notification",
        positive_help="Notify the target after updating access when supported.",
        negative_help="Do not notify the target after updating access when supported.",
    )
    member_update.add_argument("--params-json")
    member_update.add_argument("--data-json")

    member_delete = member_sub.add_parser("delete")
    member_delete.add_argument("--token", dest="identifier", required=True)
    member_delete.add_argument("--doc-type", choices=PERMISSION_DOC_TYPES)
    member_delete.add_argument("--member-id", required=True)
    member_delete.add_argument("--member-type")
    member_delete.add_argument("--perm-type")
    member_delete.add_argument("--collaborator-type")
    member_delete.add_argument("--params-json")
    member_delete.add_argument("--data-json")

    member_transfer_owner = member_sub.add_parser("transfer_owner")
    member_transfer_owner.add_argument("--token", dest="identifier", required=True)
    member_transfer_owner.add_argument("--doc-type", choices=PERMISSION_DOC_TYPES)
    member_transfer_owner.add_argument("--member-type")
    member_transfer_owner.add_argument("--member-id")
    owner_notification_group = member_transfer_owner.add_mutually_exclusive_group()
    owner_notification_group.add_argument("--need-notification", dest="need_notification", action="store_true")
    owner_notification_group.add_argument("--no-need-notification", dest="need_notification", action="store_false")
    member_transfer_owner.set_defaults(need_notification=None)
    owner_group = member_transfer_owner.add_mutually_exclusive_group()
    owner_group.add_argument("--remove-old-owner", dest="remove_old_owner", action="store_true")
    owner_group.add_argument("--keep-old-owner", dest="remove_old_owner", action="store_false")
    member_transfer_owner.set_defaults(remove_old_owner=None)
    member_transfer_owner.add_argument("--old-owner-perm")
    member_transfer_owner.add_argument("--params-json")
    member_transfer_owner.add_argument("--data-json")

    public = permission_sub.add_parser("public", help="Public permission settings.")
    public_sub = public.add_subparsers(dest="action", required=True)

    public_get = public_sub.add_parser("get")
    public_get.add_argument("--token", dest="identifier", required=True)
    public_get.add_argument("--doc-type", choices=PERMISSION_DOC_TYPES)
    public_get.add_argument("--api-version", choices=PUBLIC_PERMISSION_API_VERSIONS, default="v2")
    public_get.add_argument("--params-json")

    public_patch = public_sub.add_parser("patch")
    public_patch.add_argument("--token", dest="identifier", required=True)
    public_patch.add_argument("--doc-type", choices=PERMISSION_DOC_TYPES)
    public_patch.add_argument("--api-version", choices=PUBLIC_PERMISSION_API_VERSIONS, default="v2")
    _add_optional_bool_flag(public_patch, "external_access")
    _add_optional_bool_flag(public_patch, "invite_external")
    public_patch.add_argument("--external-access-entity")
    public_patch.add_argument("--security-entity")
    public_patch.add_argument("--comment-entity")
    public_patch.add_argument("--share-entity")
    public_patch.add_argument("--link-share-entity")
    public_patch.add_argument("--params-json")
    public_patch.add_argument("--data-json")

    public_password = public_sub.add_parser("password")
    password_sub = public_password.add_subparsers(dest="password_action", required=True)
    for action_name in ("create", "update", "delete"):
        password_action = password_sub.add_parser(action_name)
        password_action.add_argument("--token", dest="identifier", required=True)
        password_action.add_argument("--doc-type", choices=PERMISSION_DOC_TYPES)
        password_action.add_argument("--params-json")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with common.FeishuAPI(MODULE_NAME, EXPECTED_SCOPES) as api:
        if args.resource == "check":
            return _check(api, args)
        if args.resource == "drive":
            return _handle_drive(api, args)
        if args.resource == "doc":
            return _handle_doc(api, args)
        if args.resource == "wiki":
            return _handle_wiki(api, args)
        if args.resource == "permission":
            return _handle_permission(api, args)
    raise common.SkillError("validation_error", f"Unsupported resource: {args.resource}")


if __name__ == "__main__":
    raise SystemExit(common.run_cli(lambda: main()))
