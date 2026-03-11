#!/usr/bin/env python3
"""CLI for Feishu bitable operations."""

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


MODULE_NAME = "bitable"
EXPECTED_SCOPES = [
    "查看、评论和导出多维表格",
    "查看、评论、编辑和管理多维表格",
]


def _list_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "tables", "views", "fields", "records"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _require_app_table(args: argparse.Namespace, *, require_table: bool = False) -> tuple[str, str | None, str | None]:
    ids = common.normalize_bitable_ids(
        app_token=args.app_token,
        table_id=getattr(args, "table_id", None),
        view_id=getattr(args, "view_id", None),
        url=getattr(args, "url", None),
    )
    app_token = ids["app_token"]
    table_id = ids["table_id"]
    view_id = ids["view_id"]
    if not app_token:
        raise common.SkillError("validation_error", "app_token is required. Provide --app-token or a supported --url.")
    if require_table and not table_id:
        raise common.SkillError("validation_error", "table_id is required. Provide --table-id or a supported --url.")
    return app_token, table_id, view_id


def _require_json(value: str | None, *, field_name: str) -> dict[str, Any] | list[Any]:
    parsed = common.parse_json_arg(value, field_name=field_name)
    if parsed is None:
        raise common.SkillError("validation_error", f"{field_name} is required.")
    if not isinstance(parsed, (dict, list)):
        raise common.SkillError("validation_error", f"{field_name} must decode to a JSON object or array.")
    return parsed


def _find_item(items: list[dict[str, Any]], candidates: list[str], identifier: str) -> dict[str, Any]:
    for item in items:
        for key in candidates:
            if item.get(key) == identifier:
                return item
    raise common.SkillError("not_found", f"Could not find {identifier} in list response.", status=404)


def _success(api: common.FeishuAPI, args: argparse.Namespace, payload: dict[str, Any]) -> int:
    return common.output_success(
        payload.get("data"),
        paging=common.clean_paging(payload),
        meta={
            "module": MODULE_NAME,
            "resource": getattr(args, "resource", None),
            "action": getattr(args, "action", None) or "check",
            "auth_source": api.auth_metadata()["auth_source"],
        },
    )


def _check(api: common.FeishuAPI, args: argparse.Namespace) -> int:
    if args.app_token or args.url:
        app_token, _, _ = _require_app_table(args)
        payload = api.request("GET", f"/open-apis/bitable/v1/apps/{app_token}")
        probe = {
            "mode": "target_read",
            "app_token": app_token,
            "summary": {
                "name": payload.get("data", {}).get("name"),
                "revision": payload.get("data", {}).get("revision"),
            },
        }
    else:
        api.ensure_token()
        probe = {
            "mode": "auth_only",
            "message": "Provide --app-token or --url for a target-level bitable probe.",
        }
    return common.output_success(
        {
            **api.auth_metadata(),
            "expected_scopes": EXPECTED_SCOPES,
            "probe": probe,
        },
        meta={"module": MODULE_NAME, "action": "check"},
    )


def _handle_app(api: common.FeishuAPI, args: argparse.Namespace) -> int:
    if args.action != "get":
        raise common.SkillError("validation_error", "app only supports the get action.")
    app_token, _, _ = _require_app_table(args)
    payload = api.request("GET", f"/open-apis/bitable/v1/apps/{app_token}")
    return _success(api, args, payload)


def _handle_table(api: common.FeishuAPI, args: argparse.Namespace) -> int:
    app_token, table_id, _ = _require_app_table(args, require_table=args.action == "get")
    page_size = common.MAX_PAGE_SIZE if args.action == "get" else common.parse_page_size(getattr(args, "page_size", None))
    payload = api.request(
        "GET",
        f"/open-apis/bitable/v1/apps/{app_token}/tables",
        params={
            "page_size": page_size,
            "page_token": getattr(args, "page_token", None),
        },
    )
    if args.action == "list":
        return _success(api, args, payload)
    item = _find_item(_list_items(payload.get("data", {})), ["table_id"], table_id)
    return common.output_success(
        item,
        meta={"module": MODULE_NAME, "resource": "table", "action": "get", "auth_source": api.auth_metadata()["auth_source"]},
    )


def _handle_view(api: common.FeishuAPI, args: argparse.Namespace) -> int:
    app_token, table_id, view_id = _require_app_table(args, require_table=True)
    if args.action == "list":
        payload = api.request(
            "GET",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/views",
            params={
                "page_size": common.parse_page_size(getattr(args, "page_size", None)),
                "page_token": getattr(args, "page_token", None),
            },
        )
        return _success(api, args, payload)
    if not view_id:
        raise common.SkillError("validation_error", "view_id is required. Provide --view-id or a supported --url.")
    payload = api.request(
        "GET",
        f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/views/{view_id}",
    )
    return _success(api, args, payload)


def _handle_field(api: common.FeishuAPI, args: argparse.Namespace) -> int:
    app_token, table_id, _ = _require_app_table(args, require_table=True)
    if args.action == "list":
        payload = api.request(
            "GET",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            params={
                "page_size": common.parse_page_size(getattr(args, "page_size", None)),
                "page_token": getattr(args, "page_token", None),
            },
        )
        return _success(api, args, payload)
    if args.action == "get":
        if not args.field_id:
            raise common.SkillError("validation_error", "field_id is required for field get.")
        payload = api.request(
            "GET",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            params={"page_size": common.MAX_PAGE_SIZE},
        )
        item = _find_item(_list_items(payload.get("data", {})), ["field_id"], args.field_id)
        return common.output_success(
            item,
            meta={"module": MODULE_NAME, "resource": "field", "action": "get", "auth_source": api.auth_metadata()["auth_source"]},
        )
    if args.action == "create":
        body = _require_json(args.data_json, field_name="data_json")
        payload = api.request("POST", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields", json_body=body)
        return _success(api, args, payload)
    if args.action == "update":
        if not args.field_id:
            raise common.SkillError("validation_error", "field_id is required for field update.")
        body = _require_json(args.data_json, field_name="data_json")
        payload = api.request(
            "PUT",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{args.field_id}",
            json_body=body,
        )
        return _success(api, args, payload)
    if args.action == "delete":
        if not args.field_id:
            raise common.SkillError("validation_error", "field_id is required for field delete.")
        payload = api.request(
            "DELETE",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{args.field_id}",
        )
        return _success(api, args, payload)
    raise common.SkillError("validation_error", f"Unsupported field action: {args.action}")


def _handle_record(api: common.FeishuAPI, args: argparse.Namespace) -> int:
    app_token, table_id, view_id = _require_app_table(args, require_table=True)
    if args.action == "list":
        payload = api.request(
            "GET",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            params={
                "page_size": common.parse_page_size(getattr(args, "page_size", None)),
                "page_token": getattr(args, "page_token", None),
                "view_id": view_id,
                "filter": getattr(args, "filter", None),
                "sort": getattr(args, "sort", None),
                "field_names": getattr(args, "field_names", None),
            },
        )
        return _success(api, args, payload)
    if args.action == "get":
        if not args.record_id:
            raise common.SkillError("validation_error", "record_id is required for record get.")
        payload = api.request(
            "GET",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{args.record_id}",
        )
        return _success(api, args, payload)
    if args.action == "create":
        body = _require_json(args.data_json, field_name="data_json")
        payload = api.request("POST", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records", json_body=body)
        return _success(api, args, payload)
    if args.action == "update":
        if not args.record_id:
            raise common.SkillError("validation_error", "record_id is required for record update.")
        body = _require_json(args.data_json, field_name="data_json")
        payload = api.request(
            "PUT",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{args.record_id}",
            json_body=body,
        )
        return _success(api, args, payload)
    if args.action == "delete":
        if not args.record_id:
            raise common.SkillError("validation_error", "record_id is required for record delete.")
        payload = api.request(
            "DELETE",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{args.record_id}",
        )
        return _success(api, args, payload)
    if args.action == "batch_create":
        body = _require_json(args.data_json, field_name="data_json")
        payload = api.request(
            "POST",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
            json_body=body,
        )
        return _success(api, args, payload)
    if args.action == "batch_update":
        body = _require_json(args.data_json, field_name="data_json")
        payload = api.request(
            "POST",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update",
            json_body=body,
        )
        return _success(api, args, payload)
    if args.action == "batch_delete":
        body = _require_json(args.data_json, field_name="data_json")
        payload = api.request(
            "POST",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete",
            json_body=body,
        )
        return _success(api, args, payload)
    raise common.SkillError("validation_error", f"Unsupported record action: {args.action}")


def build_parser() -> argparse.ArgumentParser:
    parser = common.build_parser("bitable.py", "Operate Feishu bitable resources.")
    subparsers = parser.add_subparsers(dest="resource", required=True)

    check = subparsers.add_parser("check", help="Validate auth and optional target access.")
    check.add_argument("--app-token")
    check.add_argument("--url")

    app = subparsers.add_parser("app", help="Bitable app operations.")
    app_sub = app.add_subparsers(dest="action", required=True)
    app_get = app_sub.add_parser("get")
    app_get.add_argument("--app-token")
    app_get.add_argument("--url")

    table = subparsers.add_parser("table", help="Bitable table operations.")
    table_sub = table.add_subparsers(dest="action", required=True)
    for action in ("list", "get"):
        p = table_sub.add_parser(action)
        p.add_argument("--app-token")
        p.add_argument("--table-id")
        p.add_argument("--url")
        if action == "list":
            p.add_argument("--page-size", type=int)
            p.add_argument("--page-token")

    view = subparsers.add_parser("view", help="Bitable view operations.")
    view_sub = view.add_subparsers(dest="action", required=True)
    for action in ("list", "get"):
        p = view_sub.add_parser(action)
        p.add_argument("--app-token")
        p.add_argument("--table-id")
        p.add_argument("--view-id")
        p.add_argument("--url")
        if action == "list":
            p.add_argument("--page-size", type=int)
            p.add_argument("--page-token")

    field = subparsers.add_parser("field", help="Bitable field operations.")
    field_sub = field.add_subparsers(dest="action", required=True)
    for action in ("list", "get", "create", "update", "delete"):
        p = field_sub.add_parser(action)
        p.add_argument("--app-token")
        p.add_argument("--table-id")
        p.add_argument("--field-id")
        p.add_argument("--url")
        if action == "list":
            p.add_argument("--page-size", type=int)
            p.add_argument("--page-token")
        if action in {"create", "update"}:
            p.add_argument("--data-json")

    record = subparsers.add_parser("record", help="Bitable record operations.")
    record_sub = record.add_subparsers(dest="action", required=True)
    for action in ("list", "get", "create", "update", "delete", "batch_create", "batch_update", "batch_delete"):
        p = record_sub.add_parser(action)
        p.add_argument("--app-token")
        p.add_argument("--table-id")
        p.add_argument("--record-id")
        p.add_argument("--view-id")
        p.add_argument("--url")
        if action == "list":
            p.add_argument("--page-size", type=int)
            p.add_argument("--page-token")
            p.add_argument("--filter")
            p.add_argument("--sort")
            p.add_argument("--field-names")
        if action in {"create", "update", "batch_create", "batch_update", "batch_delete"}:
            p.add_argument("--data-json")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    with common.FeishuAPI(MODULE_NAME, EXPECTED_SCOPES) as api:
        if args.resource == "check":
            return _check(api, args)
        if args.resource == "app":
            return _handle_app(api, args)
        if args.resource == "table":
            return _handle_table(api, args)
        if args.resource == "view":
            return _handle_view(api, args)
        if args.resource == "field":
            return _handle_field(api, args)
        if args.resource == "record":
            return _handle_record(api, args)
    raise common.SkillError("validation_error", f"Unsupported resource: {args.resource}")


if __name__ == "__main__":
    raise SystemExit(common.run_cli(lambda: main()))
