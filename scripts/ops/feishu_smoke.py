#!/usr/bin/env python3
"""Feishu production smoke scenarios for OAuth, data tools, audit, and memory writeback."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nanobot.agent.memory_worker import MemoryTurnTask, MemoryWriteWorker  # noqa: E402
from nanobot.agent.tools.feishu_data.client import FeishuDataClient  # noqa: E402
from nanobot.agent.tools.feishu_data.endpoints import FeishuEndpoints  # noqa: E402
from nanobot.agent.tools.feishu_data.message_history import MessageHistoryListTool  # noqa: E402
from nanobot.agent.tools.feishu_data.token_manager import TenantAccessTokenManager  # noqa: E402
from nanobot.config.loader import load_config  # noqa: E402
from nanobot.oauth import (  # noqa: E402
    FeishuOAuthClient,
    FeishuOAuthService,
    FeishuUserTokenManager,
)
from nanobot.storage import SQLiteStore  # noqa: E402
from nanobot.storage.audit import AuditSink  # noqa: E402


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _load_runtime() -> tuple[Any, SQLiteStore]:
    config = load_config()
    state_db_path = config.resolve_feishu_state_db_path()
    sqlite_options = config.resolve_feishu_sqlite_options()
    store = SQLiteStore(state_db_path, options=sqlite_options)
    return config, store


def _resolve_redirect_uri(config: Any) -> str:
    oauth_cfg = config.integrations.feishu.oauth
    callback_path = str(oauth_cfg.callback_path or "/oauth/feishu/callback").strip() or "/oauth/feishu/callback"
    if not callback_path.startswith("/"):
        callback_path = f"/{callback_path}"

    public_base_url = str(oauth_cfg.public_base_url or "").strip().rstrip("/")
    if not public_base_url:
        raise RuntimeError("integrations.feishu.oauth.public_base_url is required")

    parsed = urlparse(public_base_url)
    if oauth_cfg.enforce_https_public_base_url and str(parsed.scheme or "").lower() != "https":
        raise RuntimeError("public_base_url must use HTTPS when enforce_https_public_base_url=true")

    host = str(parsed.hostname or "").lower().strip()
    allowlist = [
        str(item).strip().lower().lstrip(".")
        for item in (oauth_cfg.allowed_redirect_domains or [])
        if str(item).strip()
    ]
    if allowlist and not any(host == domain or host.endswith(f".{domain}") for domain in allowlist):
        raise RuntimeError("public_base_url host is not in allowed_redirect_domains")

    return f"{public_base_url}{callback_path}"


def _build_oauth_components(config: Any, store: SQLiteStore) -> tuple[FeishuOAuthService, FeishuUserTokenManager]:
    oauth_cfg = config.integrations.feishu.oauth
    auth_cfg = config.resolve_feishu_auth()
    if not oauth_cfg.enabled:
        raise RuntimeError("integrations.feishu.oauth.enabled is false")
    if not auth_cfg.app_id or not auth_cfg.app_secret:
        raise RuntimeError("missing integrations.feishu.auth.app_id/app_secret")

    redirect_uri = _resolve_redirect_uri(config)
    client = FeishuOAuthClient(
        api_base=config.resolve_feishu_api_base(),
        app_id=auth_cfg.app_id,
        app_secret=auth_cfg.app_secret,
    )
    service = FeishuOAuthService(
        store=store,
        client=client,
        redirect_uri=redirect_uri,
        scopes=list(oauth_cfg.scopes or []),
        state_ttl_seconds=int(oauth_cfg.state_ttl_seconds),
    )
    token_manager = FeishuUserTokenManager(
        store=store,
        client=client,
        refresh_ahead_seconds=int(oauth_cfg.refresh_ahead_seconds),
    )
    return service, token_manager


def _build_data_client(config: Any, store: SQLiteStore) -> FeishuDataClient:
    tool_cfg = config.tools.feishu_data
    if not tool_cfg.enabled:
        raise RuntimeError("tools.feishu_data.enabled is false")
    if not tool_cfg.app_id or not tool_cfg.app_secret:
        raise RuntimeError("missing tools.feishu_data.app_id/app_secret (or shared auth config)")
    token_manager = TenantAccessTokenManager(config=tool_cfg, sqlite_store=store)
    return FeishuDataClient(tool_cfg, token_manager=token_manager)


def oauth_smoke(args: argparse.Namespace) -> int:
    config, store = _load_runtime()
    try:
        service, token_manager = _build_oauth_components(config, store)

        auth_url = service.create_authorization_url(
            actor_open_id=args.actor_open_id,
            chat_id=args.chat_id,
            thread_id=args.thread_id or None,
        )
        state = str((parse_qs(urlsplit(auth_url).query).get("state") or [""])[0])
        state_row = store.get_oauth_state(state)

        result: dict[str, Any] = {
            "scenario": "oauth_smoke",
            "ok": bool(state and state_row),
            "authorization_url": auth_url,
            "state": state,
            "state_persisted": state_row is not None,
        }

        if args.auth_code:
            callback = service.handle_callback({"state": state, "code": args.auth_code})
            result["callback"] = {
                "success": callback.success,
                "status_code": callback.status_code,
                "message": callback.message,
                "open_id": callback.open_id,
            }
            if not callback.success:
                result["ok"] = False

        if args.verify_open_id:
            try:
                token = token_manager.get_valid_access_token(args.verify_open_id)
                result["refresh_check"] = {
                    "open_id": args.verify_open_id,
                    "success": bool(token),
                    "token_preview": token[:12],
                }
            except Exception as exc:
                result["refresh_check"] = {
                    "open_id": args.verify_open_id,
                    "success": False,
                    "error": str(exc),
                }
                result["ok"] = False

        _print_json(result)
        return 0 if result["ok"] else 2
    except Exception as exc:
        _print_json({"scenario": "oauth_smoke", "ok": False, "error": str(exc)})
        return 2
    finally:
        store.close()


async def bitable_flow_smoke(args: argparse.Namespace) -> int:
    config, store = _load_runtime()
    try:
        client = _build_data_client(config, store)
        bitable_defaults = config.resolve_feishu_bitable()

        app_token = str(args.app_token or bitable_defaults.default_app_token or "").strip()
        table_id = str(args.table_id or bitable_defaults.default_table_id or "").strip()
        if not app_token or not table_id:
            raise RuntimeError("app_token/table_id is required (arg or integrations.feishu.bitable defaults)")

        fields = json.loads(args.fields_json)
        if not isinstance(fields, dict) or not fields:
            raise RuntimeError("fields_json must be a non-empty JSON object")

        create_payload = await client.request(
            "POST",
            FeishuEndpoints.bitable_records(app_token, table_id),
            json_body={"fields": fields},
        )
        created_record = create_payload.get("data", {}).get("record", {})
        record_id = str(created_record.get("record_id") or "").strip()
        if not record_id:
            record_id = str(create_payload.get("data", {}).get("record_id") or "").strip()
        if not record_id:
            raise RuntimeError("create response missing record_id")

        get_payload = await client.request(
            "GET",
            FeishuEndpoints.bitable_record(app_token, table_id, record_id),
        )
        fetched_record = get_payload.get("data", {}).get("record", {})
        fetched_fields = fetched_record.get("fields", {}) if isinstance(fetched_record, dict) else {}
        field_match = all(fetched_fields.get(k) == v for k, v in fields.items())

        cleanup_error = None
        if args.cleanup:
            try:
                await client.request(
                    "DELETE",
                    FeishuEndpoints.bitable_record(app_token, table_id, record_id),
                )
            except Exception as exc:  # pragma: no cover - best effort cleanup
                cleanup_error = str(exc)

        result = {
            "scenario": "bitable_flow_smoke",
            "ok": field_match,
            "app_token": app_token,
            "table_id": table_id,
            "record_id": record_id,
            "field_match": field_match,
            "cleanup": {
                "requested": bool(args.cleanup),
                "error": cleanup_error,
            },
        }
        _print_json(result)
        return 0 if result["ok"] else 2
    except Exception as exc:
        _print_json({"scenario": "bitable_flow_smoke", "ok": False, "error": str(exc)})
        return 2
    finally:
        store.close()


async def calendar_task_sync_smoke(args: argparse.Namespace) -> int:
    config, store = _load_runtime()
    try:
        client = _build_data_client(config, store)
        key = args.idempotency_key or uuid.uuid4().hex[:8]

        create_calendar_payload = await client.request(
            "POST",
            FeishuEndpoints.calendar_list(),
            json_body={
                "summary": f"nanobot-smoke-{key}",
                "description": "calendar_task_sync_smoke",
            },
        )
        calendar = create_calendar_payload.get("data", {})
        calendar_id = str(calendar.get("calendar_id") or calendar.get("id") or "").strip()
        if not calendar_id:
            raise RuntimeError("calendar_create response missing calendar_id")

        first_update = await client.request(
            "PATCH",
            FeishuEndpoints.calendar_detail(calendar_id),
            json_body={"description": f"calendar_task_sync_smoke:{key}"},
        )
        second_update = await client.request(
            "PATCH",
            FeishuEndpoints.calendar_detail(calendar_id),
            json_body={"description": f"calendar_task_sync_smoke:{key}"},
        )

        task_create = await client.request(
            "POST",
            FeishuEndpoints.task_v2_tasks(),
            json_body={
                "summary": f"nanobot-smoke-task-{key}",
                "description": "derived from bitable flow",
            },
        )
        task = task_create.get("data", {})
        task_id = str(task.get("task_id") or task.get("id") or "").strip()
        if not task_id:
            raise RuntimeError("task_create response missing task_id")

        first_task_update = await client.request(
            "PATCH",
            FeishuEndpoints.task_v2_task(task_id),
            json_body={"status": "in_progress"},
        )
        second_task_update = await client.request(
            "PATCH",
            FeishuEndpoints.task_v2_task(task_id),
            json_body={"status": "in_progress"},
        )

        cleanup_error = None
        if args.cleanup:
            try:
                await client.request("DELETE", FeishuEndpoints.task_v2_task(task_id))
                await client.request("DELETE", FeishuEndpoints.calendar_detail(calendar_id))
            except Exception as exc:  # pragma: no cover - best effort cleanup
                cleanup_error = str(exc)

        result = {
            "scenario": "calendar_task_sync_smoke",
            "ok": bool(calendar_id and task_id),
            "idempotency_key": key,
            "calendar_id": calendar_id,
            "task_id": task_id,
            "calendar_updates": [first_update.get("code", 0), second_update.get("code", 0)],
            "task_updates": [first_task_update.get("code", 0), second_task_update.get("code", 0)],
            "cleanup": {
                "requested": bool(args.cleanup),
                "error": cleanup_error,
            },
        }
        _print_json(result)
        return 0 if result["ok"] else 2
    except Exception as exc:
        _print_json({"scenario": "calendar_task_sync_smoke", "ok": False, "error": str(exc)})
        return 2
    finally:
        store.close()


async def message_history_smoke(args: argparse.Namespace) -> int:
    config, store = _load_runtime()
    try:
        client = _build_data_client(config, store)
        oauth_service, token_manager = _build_oauth_components(config, store)
        _ = oauth_service

        tool = MessageHistoryListTool(config.tools.feishu_data, client, user_token_manager=token_manager)
        tool.set_runtime_context(
            "feishu",
            args.chat_id,
            sender_id=args.sender_open_id,
            metadata={"sender_open_id": args.sender_open_id},
        )
        payload = json.loads(
            await tool.execute(
                auth_mode="user",
                page_size=args.page_size,
                sort_type=args.sort_type,
            )
        )

        isolation_payload: dict[str, Any] | None = None
        if args.other_open_id:
            tool.set_runtime_context(
                "feishu",
                args.chat_id,
                sender_id=args.other_open_id,
                metadata={"sender_open_id": args.other_open_id},
            )
            isolation_payload = json.loads(
                await tool.execute(
                    auth_mode="user",
                    page_size=min(5, args.page_size),
                    sort_type=args.sort_type,
                )
            )

        ok = "error" not in payload
        result = {
            "scenario": "message_history_smoke",
            "ok": ok,
            "chat_id": args.chat_id,
            "sender_open_id": args.sender_open_id,
            "items_count": len(payload.get("items", [])) if isinstance(payload.get("items"), list) else 0,
            "needs_connect": bool(payload.get("needs_connect")),
            "error": payload.get("error"),
            "isolation_probe": isolation_payload,
        }
        _print_json(result)
        return 0 if result["ok"] else 2
    except Exception as exc:
        _print_json({"scenario": "message_history_smoke", "ok": False, "error": str(exc)})
        return 2
    finally:
        store.close()


async def audit_query_smoke(args: argparse.Namespace) -> int:
    config, store = _load_runtime()
    try:
        sink = AuditSink(store, enable_cleanup_task=False)
        now = datetime.now()
        old = now - timedelta(days=max(2, args.retention_days + 1))

        store.record_event_audit_batch(
            [
                {
                    "event_type": "smoke_old",
                    "event_id": "evt_old",
                    "chat_id": args.chat_id,
                    "message_id": "msg_old",
                    "payload": {"kind": "old"},
                    "created_at": old.isoformat(),
                }
            ]
        )
        await sink.log_event(
            "smoke_new",
            chat_id=args.chat_id,
            message_id="msg_new",
            payload={"kind": "new"},
        )

        store.upsert_feishu_message_index(
            "smoke_idx_old",
            chat_id=args.chat_id,
            content="old",
            source_message_id=None,
            created_at=old.isoformat(),
        )
        store.upsert_feishu_message_index(
            "smoke_idx_new",
            chat_id=args.chat_id,
            content="new",
            source_message_id=None,
            created_at=now.isoformat(),
        )

        before_rows = sink.query_event_audit(chat_id=args.chat_id, limit=20)
        cutoff = (now - timedelta(days=args.retention_days)).isoformat()
        deleted_events = sink.cleanup_event_audit_before(cutoff)
        deleted_index = sink.cleanup_feishu_message_index_before(cutoff)
        after_rows = sink.query_event_audit(chat_id=args.chat_id, limit=20)

        ok = deleted_events >= 1 and deleted_index >= 1
        result = {
            "scenario": "audit_query_smoke",
            "ok": ok,
            "retention_days": args.retention_days,
            "deleted_events": deleted_events,
            "deleted_message_index": deleted_index,
            "before_count": len(before_rows),
            "after_count": len(after_rows),
        }
        _print_json(result)
        return 0 if ok else 2
    except Exception as exc:
        _print_json({"scenario": "audit_query_smoke", "ok": False, "error": str(exc)})
        return 2
    finally:
        store.close()


async def memory_flush_smoke(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="nanobot-memory-smoke-") as tmp:
        workspace = Path(tmp)
        worker = MemoryWriteWorker(workspace, flush_threshold=max(1, args.threshold))
        await worker.start()

        for index in range(max(1, args.threshold - 1)):
            await worker.enqueue(
                MemoryTurnTask(
                    channel="feishu",
                    user_id="ou_smoke",
                    chat_id="oc_smoke",
                    thread_id="omt_smoke",
                    user_text=f"turn-{index}",
                    assistant_text="ok",
                    message_id=f"msg-{index}",
                    scopes=("chat", "thread"),
                    flush_threshold=args.threshold,
                )
            )
        await asyncio.sleep(0.05)

        memory_path = workspace / "memory" / "feishu" / "chats" / "oc_smoke" / "MEMORY.md"
        threshold_gate_ok = not memory_path.exists()

        await worker.enqueue(
            MemoryTurnTask(
                channel="feishu",
                user_id="ou_smoke",
                chat_id="oc_smoke",
                thread_id="omt_smoke",
                user_text="先这样",
                assistant_text="收尾完成",
                message_id="msg-final",
                scopes=("chat", "thread"),
                force_flush=True,
                flush_threshold=args.threshold,
            )
        )
        await asyncio.sleep(0.05)
        await worker.stop()

        force_flush_ok = memory_path.exists() and "先这样" in memory_path.read_text(encoding="utf-8")
        ok = threshold_gate_ok and force_flush_ok
        _print_json(
            {
                "scenario": "memory_flush_smoke",
                "ok": ok,
                "threshold": args.threshold,
                "threshold_gate_ok": threshold_gate_ok,
                "force_flush_ok": force_flush_ok,
                "workspace": str(workspace),
            }
        )
        return 0 if ok else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Feishu production smoke scenarios")
    subparsers = parser.add_subparsers(dest="scenario", required=True)

    oauth = subparsers.add_parser("oauth_smoke", help="Check OAuth URL/state persistence/callback refresh path")
    oauth.add_argument("--actor-open-id", required=True)
    oauth.add_argument("--chat-id", required=True)
    oauth.add_argument("--thread-id")
    oauth.add_argument("--auth-code", help="Optional OAuth code to validate callback exchange")
    oauth.add_argument("--verify-open-id", help="Optional open_id to verify refresh path")

    bitable = subparsers.add_parser("bitable_flow_smoke", help="Create and read back a bitable record")
    bitable.add_argument("--app-token")
    bitable.add_argument("--table-id")
    bitable.add_argument("--fields-json", required=True, help='JSON object, e.g. {"事项":"测试"}')
    bitable.add_argument("--cleanup", action="store_true")

    sync = subparsers.add_parser(
        "calendar_task_sync_smoke",
        help="Create/update calendar and task resources with idempotent updates",
    )
    sync.add_argument("--idempotency-key")
    sync.add_argument("--cleanup", action="store_true")

    history = subparsers.add_parser(
        "message_history_smoke",
        help="Fetch message history with user OAuth context isolation",
    )
    history.add_argument("--chat-id", required=True)
    history.add_argument("--sender-open-id", required=True)
    history.add_argument("--other-open-id")
    history.add_argument("--page-size", type=int, default=20)
    history.add_argument("--sort-type", default="ByCreateTimeDesc")

    audit = subparsers.add_parser(
        "audit_query_smoke",
        help="Write/query audit events and verify retention cleanup",
    )
    audit.add_argument("--chat-id", default="oc_smoke")
    audit.add_argument("--retention-days", type=int, default=1)

    memory = subparsers.add_parser(
        "memory_flush_smoke",
        help="Stress threshold flush and topic-end force flush behavior",
    )
    memory.add_argument("--threshold", type=int, default=3)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.scenario == "oauth_smoke":
        return oauth_smoke(args)
    if args.scenario == "bitable_flow_smoke":
        return asyncio.run(bitable_flow_smoke(args))
    if args.scenario == "calendar_task_sync_smoke":
        return asyncio.run(calendar_task_sync_smoke(args))
    if args.scenario == "message_history_smoke":
        return asyncio.run(message_history_smoke(args))
    if args.scenario == "audit_query_smoke":
        return asyncio.run(audit_query_smoke(args))
    if args.scenario == "memory_flush_smoke":
        return asyncio.run(memory_flush_smoke(args))
    _print_json({"ok": False, "error": f"unknown scenario: {args.scenario}"})
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
