#!/usr/bin/env python3
"""Shared helpers for the feishu-workspace skill scripts."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

DEFAULT_TIMEOUT = 30.0
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
DEFAULT_DOC_MAX_CHARS = 8000
PERMISSION_DENIED_CODE = 99991672

BASE_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "User-Agent": "nanobot-feishu-workspace/1.0",
}


class SkillError(Exception):
    """Structured error for JSON CLI output."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        code: str | int | None = None,
        status: int | None = None,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.code = str(code) if code is not None else None
        self.status = status
        self.request_id = request_id
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        error = {
            "kind": self.kind,
            "message": self.message,
            "code": self.code,
            "status": self.status,
            "request_id": self.request_id,
        }
        if self.details:
            error["details"] = self.details
        return error


@dataclass
class AuthConfig:
    auth_source: str
    token: str | None = None
    app_id: str | None = None
    app_secret: str | None = None
    config_path: str | None = None


class FeishuAPI:
    """Thin sync wrapper around Feishu Open API with tenant auth."""

    def __init__(
        self,
        module_name: str,
        expected_scopes: list[str],
        *,
        auth_config: AuthConfig | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.module_name = module_name
        self.expected_scopes = expected_scopes
        self.auth_config = auth_config or resolve_auth_config()
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._tenant_token = self.auth_config.token
        self._expires_at: float | None = None
        self.last_auth_metadata: dict[str, Any] = {
            "auth_source": self.auth_config.auth_source,
            "token_type": "tenant_access_token",
            "expires_in": None,
            "expires_at": None,
        }

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FeishuAPI":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def auth_metadata(self) -> dict[str, Any]:
        return dict(self.last_auth_metadata)

    def ensure_token(self) -> str:
        if self._tenant_token and self._expires_at and self._expires_at > time.time() + 30:
            return self._tenant_token
        if self._tenant_token and self.auth_config.auth_source == "env:tenant_access_token":
            return self._tenant_token
        if self.auth_config.token:
            self._tenant_token = self.auth_config.token
            return self._tenant_token
        if not self.auth_config.app_id or not self.auth_config.app_secret:
            raise SkillError(
                "auth_error",
                "Missing Feishu credentials. Set FEISHU_TENANT_ACCESS_TOKEN or FEISHU_APP_ID/FEISHU_APP_SECRET, or configure channels.feishu.app_id/app_secret in nanobot config.",
                status=401,
            )
        response = self._client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            headers=BASE_HEADERS,
            json={
                "app_id": self.auth_config.app_id,
                "app_secret": self.auth_config.app_secret,
            },
        )
        payload = self._decode_json(response)
        self._raise_if_feishu_error(payload, response.status_code, self._extract_request_id(response, payload))
        token = payload.get("tenant_access_token")
        if not token:
            raise SkillError("auth_error", "Feishu auth response did not include tenant_access_token.", status=502)
        expires_in = int(payload.get("expire", 0) or 0)
        self._tenant_token = token
        self._expires_at = time.time() + expires_in
        self.last_auth_metadata = {
            "auth_source": self.auth_config.auth_source,
            "token_type": "tenant_access_token",
            "expires_in": expires_in or None,
            "expires_at": int(self._expires_at) if expires_in else None,
        }
        return token

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | list[Any] | None = None,
        module_override: str | None = None,
        expected_scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        token = self.ensure_token()
        response = self._client.request(
            method.upper(),
            f"https://open.feishu.cn{path}",
            headers={**BASE_HEADERS, "Authorization": f"Bearer {token}"},
            params=_clean_dict(params),
            json=json_body,
        )
        payload = self._decode_json(response)
        request_id = self._extract_request_id(response, payload)
        self._raise_if_feishu_error(
            payload,
            response.status_code,
            request_id,
            module_name=module_override,
            expected_scopes=expected_scopes,
        )
        return payload

    def check(
        self,
        probe_call: callable,
    ) -> dict[str, Any]:
        probe = probe_call()
        auth = self.auth_metadata()
        return {
            "ok": True,
            "data": {
                "auth_source": auth["auth_source"],
                "token_type": auth["token_type"],
                "expires_in": auth.get("expires_in"),
                "expires_at": auth.get("expires_at"),
                "expected_scopes": self.expected_scopes,
                "probe": probe,
            },
            "paging": None,
            "meta": {"module": self.module_name},
        }

    def _decode_json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            return response.json()
        except ValueError as exc:
            raise SkillError(
                "api_error",
                "Feishu API returned a non-JSON response.",
                status=response.status_code,
                details={"response_text": response.text[:1000]},
            ) from exc

    def _raise_if_feishu_error(
        self,
        payload: dict[str, Any],
        status: int,
        request_id: str | None,
        *,
        module_name: str | None = None,
        expected_scopes: list[str] | None = None,
    ) -> None:
        code = payload.get("code", 0)
        if status < 400 and code in (0, "0", None):
            return
        module_name = module_name or self.module_name
        expected_scopes = expected_scopes or self.expected_scopes
        message = payload.get("msg") or payload.get("message") or f"Feishu API request failed ({status})."
        details: dict[str, Any] = {}
        if code == PERMISSION_DENIED_CODE:
            details["expected_scopes"] = expected_scopes
            raise SkillError(
                "permission_denied",
                f"Feishu permission denied for {module_name}. Please enable the required scopes for this module in Feishu Open Platform.",
                code=code,
                status=status,
                request_id=request_id,
                details=details,
            )
        raise SkillError(
            "api_error",
            message,
            code=code,
            status=status,
            request_id=request_id,
        )

    @staticmethod
    def _extract_request_id(response: httpx.Response, payload: dict[str, Any]) -> str | None:
        for header in ("x-tt-logid", "x-request-id"):
            value = response.headers.get(header)
            if value:
                return value
        if isinstance(payload.get("request_id"), str):
            return payload["request_id"]
        if isinstance(payload.get("RequestId"), str):
            return payload["RequestId"]
        return None


def resolve_auth_config() -> AuthConfig:
    env_token = os.environ.get("FEISHU_TENANT_ACCESS_TOKEN")
    if env_token:
        return AuthConfig(auth_source="env:tenant_access_token", token=env_token)

    env_app_id = os.environ.get("FEISHU_APP_ID")
    env_app_secret = os.environ.get("FEISHU_APP_SECRET")
    if env_app_id and env_app_secret:
        return AuthConfig(
            auth_source="env:app_credentials",
            app_id=env_app_id,
            app_secret=env_app_secret,
        )

    config_creds = _load_nanobot_config_credentials()
    if config_creds:
        return config_creds

    return AuthConfig(auth_source="missing")


def _load_nanobot_config_credentials() -> AuthConfig | None:
    try:
        from nanobot.config.loader import get_config_path, load_config
    except Exception:
        return None

    try:
        config_path = get_config_path()
        config = load_config(config_path)
        app_id = config.channels.feishu.app_id
        app_secret = config.channels.feishu.app_secret
    except Exception:
        return None

    if not app_id or not app_secret:
        return None
    return AuthConfig(
        auth_source="nanobot_config",
        app_id=app_id,
        app_secret=app_secret,
        config_path=str(config_path),
    )


def output_success(
    data: Any,
    *,
    paging: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> int:
    payload = {
        "ok": True,
        "data": data,
        "paging": paging,
        "meta": meta or {},
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def output_error(error: SkillError) -> int:
    payload = {
        "ok": False,
        "error": error.to_dict(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1


def run_cli(main_func: callable) -> int:
    try:
        result = main_func()
        if isinstance(result, int):
            return result
        if isinstance(result, dict) and "ok" in result:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 1
        if result is None:
            return 0
        print(json.dumps({"ok": True, "data": result, "paging": None, "meta": {}}, ensure_ascii=False, indent=2))
        return 0
    except SkillError as exc:
        return output_error(exc)


def parse_json_arg(value: str | None, *, field_name: str) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise SkillError("validation_error", f"Invalid JSON for {field_name}: {exc}") from exc


def parse_page_size(value: int | None) -> int:
    if value is None:
        return DEFAULT_PAGE_SIZE
    if value < 1:
        raise SkillError("validation_error", "page_size must be >= 1.")
    return min(value, MAX_PAGE_SIZE)


def clean_paging(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    paging = {
        "has_more": data.get("has_more"),
        "page_token": data.get("page_token") or data.get("next_page_token"),
        "next_page_token": data.get("page_token") or data.get("next_page_token"),
    }
    if paging["has_more"] is None and paging["page_token"] is None:
        return None
    return paging


def make_check_result(api: FeishuAPI, probe: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "data": {
            **api.auth_metadata(),
            "expected_scopes": api.expected_scopes,
            "probe": probe,
        },
        "paging": None,
        "meta": {"module": api.module_name},
    }


def _clean_dict(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    result: dict[str, Any] = {}
    for key, item in value.items():
        if item is None:
            continue
        if isinstance(item, bool):
            result[key] = "true" if item else "false"
        else:
            result[key] = item
    return result


def load_text_arg(text: str | None, text_file: str | None) -> str:
    if text and text_file:
        raise SkillError("validation_error", "Use either --text or --text-file, not both.")
    if text_file:
        return Path(text_file).read_text(encoding="utf-8")
    if text is None:
        raise SkillError("validation_error", "Text content is required.")
    return text


def normalize_bitable_ids(
    *,
    app_token: str | None = None,
    table_id: str | None = None,
    view_id: str | None = None,
    url: str | None = None,
) -> dict[str, str | None]:
    if url:
        parsed = _parse_feishu_url(url)
        if parsed["kind"] != "bitable":
            raise SkillError("validation_error", f"Unsupported bitable URL: {url}")
        app_token = app_token or parsed.get("app_token")
        table_id = table_id or parsed.get("table_id")
        view_id = view_id or parsed.get("view_id")
    return {"app_token": app_token, "table_id": table_id, "view_id": view_id}


def normalize_docs_identifier(value: str) -> dict[str, str]:
    parsed = _parse_feishu_url(value)
    if parsed["kind"] == "unknown":
        if "/" not in value:
            return {"kind": "raw", "token": value}
        raise SkillError("validation_error", f"Unsupported Feishu document/wiki/drive URL: {value}")
    return parsed


def normalize_calendar_id(value: str) -> str:
    if re.match(r"^https?://", value):
        raise SkillError("validation_error", "Calendar v1 only accepts raw calendar_id/event_id, not URLs.")
    return value


def _parse_feishu_url(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "feishu.cn" not in host and "larksuite.com" not in host:
        return {"kind": "unknown"}
    path = parsed.path.strip("/")
    parts = [segment for segment in path.split("/") if segment]
    query = parse_qs(parsed.query)

    if len(parts) >= 2 and parts[0] == "base":
        return {
            "kind": "bitable",
            "app_token": parts[1],
            "table_id": _first_query(query, ["table", "tableId"]),
            "view_id": _first_query(query, ["view", "viewId"]),
        }
    if len(parts) >= 2 and parts[0] in {"docx", "docs"}:
        return {"kind": "doc", "document_id": parts[1]}
    if len(parts) >= 2 and parts[0] == "wiki":
        return {"kind": "wiki", "node_token": parts[1]}
    if len(parts) >= 2 and parts[0] == "file":
        return {"kind": "file", "file_token": parts[1]}
    if len(parts) >= 3 and parts[0] == "drive" and parts[1] == "folder":
        return {"kind": "folder", "folder_token": parts[2]}
    return {"kind": "unknown"}


def _first_query(query: dict[str, list[str]], names: list[str]) -> str | None:
    for name in names:
        values = query.get(name)
        if values:
            return values[0]
    return None


def normalize_doc_token(value: str, *, expected_kind: str) -> str:
    parsed = normalize_docs_identifier(value)
    if parsed["kind"] == "raw":
        return parsed["token"]
    token_key = {
        "doc": "document_id",
        "wiki": "node_token",
        "file": "file_token",
        "folder": "folder_token",
    }.get(expected_kind)
    if parsed["kind"] != expected_kind or not token_key or token_key not in parsed:
        raise SkillError("validation_error", f"Expected a {expected_kind} token or URL, got: {value}")
    return parsed[token_key]


def normalize_docx_or_file_token(value: str) -> tuple[str, str]:
    parsed = normalize_docs_identifier(value)
    if parsed["kind"] == "raw":
        return "raw", parsed["token"]
    mapping = {
        "doc": ("document_id", parsed["document_id"]),
        "file": ("file_token", parsed["file_token"]),
        "folder": ("folder_token", parsed["folder_token"]),
        "wiki": ("node_token", parsed["node_token"]),
    }
    if parsed["kind"] not in mapping:
        raise SkillError("validation_error", f"Unsupported docs identifier: {value}")
    return mapping[parsed["kind"]]


def text_preview(text: str, *, max_chars: int = 200) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def build_parser(prog: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("--debug", action="store_true", help="Include extra debug metadata in output.")
    return parser
