#!/usr/bin/env python3
"""CLI for Feishu docs, wiki, and drive operations."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

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


def _success(api: common.FeishuAPI, args: argparse.Namespace, payload: dict[str, Any], *, paging: dict[str, Any] | None = None) -> int:
    return common.output_success(
        payload,
        paging=paging,
        meta={
            "module": MODULE_NAME,
            "resource": getattr(args, "resource", None),
            "action": getattr(args, "action", None) or "check",
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
    raise common.SkillError("validation_error", f"Unsupported resource: {args.resource}")


if __name__ == "__main__":
    raise SystemExit(common.run_cli(lambda: main()))
