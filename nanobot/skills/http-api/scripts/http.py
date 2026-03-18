#!/usr/bin/env python3
"""Controlled HTTP wrapper for external API calls."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nanobot.security.network import validate_resolved_url, validate_url_target  # noqa: E402

DEFAULT_MAX_CHARS = 20000
DEFAULT_TIMEOUT = 30.0


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _expand_env(val) for key, val in value.items()}
    return value


def _load_json_arg(raw: str | None, *, label: str) -> Any:
    if raw is None:
        return None
    try:
        return _expand_env(json.loads(raw))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid {label}: {exc}") from exc


def _parse_headers(header_items: list[str] | None, header_json: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    parsed_json = _load_json_arg(header_json, label="header_json")
    if parsed_json is not None:
        if not isinstance(parsed_json, dict):
            raise ValueError("header_json must decode to a JSON object.")
        headers.update({str(k): str(v) for k, v in parsed_json.items()})

    for item in header_items or []:
        if ":" not in item:
            raise ValueError(f"Invalid header {item!r}; expected 'Name: Value'.")
        name, value = item.split(":", 1)
        headers[name.strip()] = os.path.expandvars(value.strip())
    return headers


def _build_request_body(args: argparse.Namespace, headers: dict[str, str]) -> bytes | None:
    if args.data_json and args.body is not None:
        raise ValueError("Use either --data-json or --body, not both.")

    if args.data_json:
        payload = _load_json_arg(args.data_json, label="data_json")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json; charset=utf-8")
        return body

    if args.body is None:
        return None

    headers.setdefault("Content-Type", args.content_type or "text/plain; charset=utf-8")
    return os.path.expandvars(args.body).encode("utf-8")


def _decode_body(body: bytes, headers: dict[str, str], max_chars: int) -> dict[str, Any]:
    text = body.decode("utf-8", errors="replace")
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]

    content_type = headers.get("Content-Type", headers.get("content-type", ""))
    payload: dict[str, Any] = {
        "body_text": text,
        "truncated": truncated,
    }

    if "json" in content_type.lower():
        try:
            payload["body_json"] = json.loads(text)
        except json.JSONDecodeError:
            pass
    return payload


def _flatten_query(query: dict[str, Any]) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for key, value in query.items():
        if isinstance(value, list):
            items.extend((str(key), "" if item is None else str(item)) for item in value)
            continue
        items.append((str(key), "" if value is None else str(value)))
    return items


def request_once(
    *,
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
    body: bytes | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_chars: int = DEFAULT_MAX_CHARS,
    allow_non_2xx: bool = False,
    opener: urllib.request.OpenerDirector | None = None,
) -> dict[str, Any]:
    expanded_url = os.path.expandvars(url)
    if query:
        query_pairs = urllib.parse.urlencode(
            _flatten_query(query),
            doseq=True,
        )
        joiner = "&" if "?" in expanded_url else "?"
        expanded_url = f"{expanded_url}{joiner}{query_pairs}"

    ok, error = validate_url_target(expanded_url)
    if not ok:
        return {
            "ok": False,
            "error": {
                "kind": "validation_error",
                "message": f"URL validation failed: {error}",
                "status": 400,
            },
        }

    req = urllib.request.Request(
        expanded_url,
        data=body,
        method=method.upper(),
        headers=headers or {},
    )
    opener = opener or urllib.request.build_opener()

    try:
        response = opener.open(req, timeout=timeout)
        close = getattr(response, "close", None)
        try:
            final_url = response.geturl()
            redir_ok, redir_err = validate_resolved_url(final_url)
            if not redir_ok:
                return {
                    "ok": False,
                    "error": {
                        "kind": "validation_error",
                        "message": redir_err,
                        "status": 400,
                    },
                }
            response_headers = dict(response.headers.items())
            body_bytes = response.read()
            status = getattr(response, "status", response.getcode())
        finally:
            if callable(close):
                close()
    except urllib.error.HTTPError as exc:
        final_url = exc.geturl()
        redir_ok, redir_err = validate_resolved_url(final_url)
        if not redir_ok:
            return {
                "ok": False,
                "error": {
                    "kind": "validation_error",
                    "message": redir_err,
                    "status": 400,
                },
            }
        response_headers = dict(exc.headers.items())
        body_bytes = exc.read()
        status = exc.code
        if not allow_non_2xx:
            decoded = _decode_body(body_bytes, response_headers, max_chars)
            return {
                "ok": False,
                "error": {
                    "kind": "http_error",
                    "message": f"HTTP {status}",
                    "status": status,
                    "url": final_url,
                    "request_id": response_headers.get("x-tt-logid") or response_headers.get("x-request-id"),
                    "body_text": decoded["body_text"],
                    "truncated": decoded["truncated"],
                },
            }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "error": {
                "kind": "network_error",
                "message": str(exc.reason),
                "status": 502,
            },
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "kind": "request_error",
                "message": str(exc),
                "status": 500,
            },
        }

    decoded = _decode_body(body_bytes, response_headers, max_chars)
    return {
        "ok": True,
        "data": {
            "method": method.upper(),
            "url": expanded_url,
            "final_url": final_url,
            "status": status,
            "headers": response_headers,
            "request_id": response_headers.get("x-tt-logid") or response_headers.get("x-request-id"),
            **decoded,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Controlled HTTP API helper.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    request = subparsers.add_parser("request", help="Send an HTTP request.")
    request.add_argument("--method", default="GET")
    request.add_argument("--url", required=True)
    request.add_argument("--header", action="append", default=[])
    request.add_argument("--header-json")
    request.add_argument("--query-json")
    request.add_argument("--data-json")
    request.add_argument("--body")
    request.add_argument("--content-type")
    request.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    request.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    request.add_argument("--allow-non-2xx", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        headers = _parse_headers(args.header, args.header_json)
        body = _build_request_body(args, headers)
        query = _load_json_arg(args.query_json, label="query_json")
        if query is not None and not isinstance(query, dict):
            raise ValueError("query_json must decode to a JSON object.")
    except ValueError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "kind": "validation_error",
                        "message": str(exc),
                        "status": 400,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    result = request_once(
        method=args.method,
        url=args.url,
        headers=headers,
        query=query,
        body=body,
        timeout=args.timeout,
        max_chars=args.max_chars,
        allow_non_2xx=args.allow_non_2xx,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
