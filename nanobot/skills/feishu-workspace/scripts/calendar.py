#!/usr/bin/env python3
"""CLI for Feishu calendar operations."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path = [entry for entry in sys.path if Path(entry or ".").resolve() != SCRIPT_DIR]
COMMON_SPEC = importlib.util.spec_from_file_location("feishu_workspace_skill_common", SCRIPT_DIR / "common.py")
assert COMMON_SPEC and COMMON_SPEC.loader
common = importlib.util.module_from_spec(COMMON_SPEC)
sys.modules.setdefault("feishu_workspace_skill_common", common)
COMMON_SPEC.loader.exec_module(common)


MODULE_NAME = "calendar"
EXPECTED_SCOPES = [
    "获取日历、日程及忙闲信息",
    "更新日历及日程信息",
]
CALENDAR_LIST_MIN_PAGE_SIZE = 50


def _parse_iso_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise common.SkillError("validation_error", f"Invalid ISO 8601 datetime: {value}") from exc
    if dt.tzinfo is None:
        raise common.SkillError("validation_error", f"Datetime must include timezone information: {value}")
    return dt


def _normalize_time_query(value: str | None) -> str | None:
    if value is None or value.isdigit():
        return value
    return str(int(_parse_iso_datetime(value).timestamp()))


def _normalize_event_time_info(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    dt = _parse_iso_datetime(value)
    return {
        "timestamp": str(int(dt.timestamp())),
        "timezone": "UTC",
    }


def _normalize_event_body(body: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(body)
    for key in ("start_time", "end_time"):
        if key in normalized:
            normalized[key] = _normalize_event_time_info(normalized[key])
    return normalized


def _calendar_page_size(value: int | None) -> int:
    return max(common.parse_page_size(value), CALENDAR_LIST_MIN_PAGE_SIZE)


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


def _require_calendar_id(args: argparse.Namespace) -> str:
    if not args.calendar_id:
        raise common.SkillError("validation_error", "calendar_id is required.")
    return common.normalize_calendar_id(args.calendar_id)


def _check(api: common.FeishuAPI, args: argparse.Namespace) -> int:
    if args.calendar_id:
        calendar_id = common.normalize_calendar_id(args.calendar_id)
        payload = api.request("GET", f"/open-apis/calendar/v4/calendars/{calendar_id}")
        probe = {
            "mode": "target_read",
            "calendar_id": calendar_id,
            "summary": {
                "summary": payload.get("data", {}).get("summary") or payload.get("data", {}).get("calendar", {}).get("summary"),
            },
        }
    else:
        payload = api.request("GET", "/open-apis/calendar/v4/calendars")
        calendars = payload.get("data", {}).get("calendar_list") or payload.get("data", {}).get("items") or []
        probe = {
            "mode": "module_read",
            "visible_count": len(calendars),
            "sample": calendars[:1],
        }
    return common.output_success(
        {
            **api.auth_metadata(),
            "expected_scopes": EXPECTED_SCOPES,
            "probe": probe,
        },
        meta={"module": MODULE_NAME, "action": "check"},
    )


def _handle_calendar(api: common.FeishuAPI, args: argparse.Namespace) -> int:
    if args.action == "list":
        payload = api.request(
            "GET",
            "/open-apis/calendar/v4/calendars",
            params={"page_size": _calendar_page_size(args.page_size), "page_token": args.page_token},
        )
        return _success(api, args, payload)
    if args.action == "get":
        calendar_id = _require_calendar_id(args)
        payload = api.request("GET", f"/open-apis/calendar/v4/calendars/{calendar_id}")
        return _success(api, args, payload)
    raise common.SkillError("validation_error", f"Unsupported calendar action: {args.action}")


def _handle_event(api: common.FeishuAPI, args: argparse.Namespace) -> int:
    calendar_id = _require_calendar_id(args)
    if args.action == "list":
        payload = api.request(
            "GET",
            f"/open-apis/calendar/v4/calendars/{calendar_id}/events",
            params={
                "page_size": common.parse_page_size(args.page_size),
                "page_token": args.page_token,
                "start_time": _normalize_time_query(args.start_time),
                "end_time": _normalize_time_query(args.end_time),
                "anchor_time": _normalize_time_query(args.anchor_time),
                "sync_token": args.sync_token,
            },
        )
        return _success(api, args, payload)
    event_id = getattr(args, "event_id", None)
    if not event_id and args.action in {"get", "update", "delete"}:
        raise common.SkillError("validation_error", "event_id is required.")
    if args.action == "get":
        payload = api.request(
            "GET",
            f"/open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}",
            params={"need_attendee": args.need_attendee, "need_meeting_settings": args.need_meeting_settings},
        )
        return _success(api, args, payload)
    if args.action == "create":
        body = common.parse_json_arg(args.data_json, field_name="data_json")
        if not isinstance(body, dict):
            raise common.SkillError("validation_error", "data_json must be a JSON object.")
        body = _normalize_event_body(body)
        payload = api.request(
            "POST",
            f"/open-apis/calendar/v4/calendars/{calendar_id}/events",
            params={"idempotency_key": args.idempotency_key},
            json_body=body,
        )
        return _success(api, args, payload)
    if args.action == "update":
        body = common.parse_json_arg(args.data_json, field_name="data_json")
        if not isinstance(body, dict):
            raise common.SkillError("validation_error", "data_json must be a JSON object.")
        body = _normalize_event_body(body)
        payload = api.request(
            "PATCH",
            f"/open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}",
            json_body=body,
        )
        return _success(api, args, payload)
    if args.action == "delete":
        payload = api.request(
            "DELETE",
            f"/open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}",
            params={"need_notification": args.need_notification},
        )
        return _success(api, args, payload)
    raise common.SkillError("validation_error", f"Unsupported event action: {args.action}")


def build_parser() -> argparse.ArgumentParser:
    parser = common.build_parser("calendar.py", "Operate Feishu calendar resources.")
    subparsers = parser.add_subparsers(dest="resource", required=True)

    check = subparsers.add_parser("check", help="Validate auth and optional calendar access.")
    check.add_argument("--calendar-id")

    calendar = subparsers.add_parser("calendar", help="Calendar operations.")
    calendar_sub = calendar.add_subparsers(dest="action", required=True)
    cal_list = calendar_sub.add_parser("list")
    cal_list.add_argument("--page-size", type=int)
    cal_list.add_argument("--page-token")
    cal_get = calendar_sub.add_parser("get")
    cal_get.add_argument("--calendar-id", required=True)

    event = subparsers.add_parser("event", help="Calendar event operations.")
    event_sub = event.add_subparsers(dest="action", required=True)
    event_list = event_sub.add_parser("list")
    event_list.add_argument("--calendar-id", required=True)
    event_list.add_argument("--page-size", type=int)
    event_list.add_argument("--page-token")
    event_list.add_argument("--start-time")
    event_list.add_argument("--end-time")
    event_list.add_argument("--anchor-time")
    event_list.add_argument("--sync-token")

    event_get = event_sub.add_parser("get")
    event_get.add_argument("--calendar-id", required=True)
    event_get.add_argument("--event-id", required=True)
    event_get.add_argument("--need-attendee", action="store_true")
    event_get.add_argument("--need-meeting-settings", action="store_true")

    event_create = event_sub.add_parser("create")
    event_create.add_argument("--calendar-id", required=True)
    event_create.add_argument("--data-json", required=True)
    event_create.add_argument("--idempotency-key")

    event_update = event_sub.add_parser("update")
    event_update.add_argument("--calendar-id", required=True)
    event_update.add_argument("--event-id", required=True)
    event_update.add_argument("--data-json", required=True)

    event_delete = event_sub.add_parser("delete")
    event_delete.add_argument("--calendar-id", required=True)
    event_delete.add_argument("--event-id", required=True)
    event_delete.add_argument("--need-notification")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with common.FeishuAPI(MODULE_NAME, EXPECTED_SCOPES) as api:
        if args.resource == "check":
            return _check(api, args)
        if args.resource == "calendar":
            return _handle_calendar(api, args)
        if args.resource == "event":
            return _handle_event(api, args)
    raise common.SkillError("validation_error", f"Unsupported resource: {args.resource}")


if __name__ == "__main__":
    raise SystemExit(common.run_cli(lambda: main()))
