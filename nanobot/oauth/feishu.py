"""Feishu OAuth business services and token lifecycle manager."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from nanobot.storage.sqlite_store import SQLiteStore


class FeishuOAuthError(RuntimeError):
    """Raised when Feishu OAuth API returns an error."""

    def __init__(self, message: str, *, code: str | int | None = None, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class FeishuReauthorizationRequired(RuntimeError):
    """Raised when user authorization is missing or no longer refreshable."""


@dataclass
class OAuthCallbackResult:
    """Structured callback handling result."""

    success: bool
    status_code: int
    message: str
    open_id: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None


class FeishuOAuthClient:
    """Thin HTTP client for Feishu OAuth endpoints."""

    AUTHORIZE_PATH = "/open-apis/authen/v1/authorize"
    TOKEN_PATH = "/open-apis/authen/v2/oauth/token"
    USER_INFO_PATH = "/open-apis/authen/v1/user_info"

    def __init__(
        self,
        *,
        api_base: str,
        app_id: str,
        app_secret: str,
        http_client_factory: Callable[..., Any] | None = None,
    ):
        self.api_base = api_base.rstrip("/")
        self.app_id = app_id
        self.app_secret = app_secret
        self._http_client_factory = http_client_factory or httpx.Client

    def build_authorization_url(self, *, redirect_uri: str, state: str, scopes: list[str]) -> str:
        params = {
            "app_id": self.app_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
        if scopes:
            params["scope"] = " ".join(scopes)
        return f"{self.api_base}{self.AUTHORIZE_PATH}?{urlencode(params)}"

    def exchange_code(self, *, code: str, redirect_uri: str) -> dict[str, Any]:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "redirect_uri": redirect_uri,
        }
        return self._post_token(payload)

    def refresh_access_token(self, *, refresh_token: str) -> dict[str, Any]:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.app_id,
            "client_secret": self.app_secret,
        }
        return self._post_token(payload)

    def get_user_info(self, *, access_token: str) -> dict[str, Any]:
        url = f"{self.api_base}{self.USER_INFO_PATH}"
        headers = {"Authorization": f"Bearer {access_token}"}
        with self._http_client_factory(timeout=15.0) as client:
            response = client.get(url, headers=headers)
        return self._parse_response(response)

    def _post_token(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.api_base}{self.TOKEN_PATH}"
        with self._http_client_factory(timeout=15.0) as client:
            response = client.post(url, json=payload)
        return self._parse_response(response)

    @staticmethod
    def _parse_response(response: httpx.Response) -> dict[str, Any]:
        status = response.status_code
        try:
            data = response.json()
        except ValueError as exc:
            raise FeishuOAuthError(
                f"Invalid JSON response from Feishu OAuth: {response.text[:200]}",
                status_code=status,
            ) from exc

        if status >= 400:
            raise FeishuOAuthError(
                str(data.get("error_description") or data.get("msg") or data),
                code=data.get("code") or data.get("error"),
                status_code=status,
            )

        if isinstance(data, dict) and data.get("code") not in (None, 0):
            raise FeishuOAuthError(
                str(data.get("error_description") or data.get("msg") or "Feishu OAuth error"),
                code=data.get("code") or data.get("error"),
                status_code=status,
            )
        if isinstance(data, dict) and data.get("error"):
            raise FeishuOAuthError(
                str(data.get("error_description") or data.get("error") or "Feishu OAuth error"),
                code=data.get("code") or data.get("error"),
                status_code=status,
            )

        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        if not isinstance(payload, dict):
            raise FeishuOAuthError("Unexpected OAuth payload shape", status_code=status)
        return payload


class FeishuOAuthService:
    """Coordinates state lifecycle, callback handling, and token persistence."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        client: FeishuOAuthClient,
        redirect_uri: str,
        scopes: list[str],
        state_ttl_seconds: int = 600,
    ):
        self.store = store
        self.client = client
        self.redirect_uri = redirect_uri
        self.scopes = [scope.strip() for scope in scopes if scope.strip()]
        self.state_ttl_seconds = max(60, int(state_ttl_seconds))

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().isoformat()

    def create_authorization_url(
        self,
        *,
        actor_open_id: str,
        chat_id: str,
        thread_id: str | None = None,
        scopes: list[str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        now = datetime.now()
        state = secrets.token_urlsafe(32)
        merged_scopes = [scope.strip() for scope in (scopes or self.scopes) if scope.strip()]
        self.store.upsert_oauth_state(
            state,
            provider="feishu",
            actor_open_id=actor_open_id,
            chat_id=chat_id,
            thread_id=thread_id,
            redirect_uri=self.redirect_uri,
            scopes=merged_scopes,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=self.state_ttl_seconds)).isoformat(),
            payload=payload,
        )
        self.store.record_event_audit(
            "oauth_authorization_created",
            chat_id=chat_id,
            payload={
                "provider": "feishu",
                "actor_open_id": actor_open_id,
                "thread_id": thread_id,
                "state": state,
                "scopes": merged_scopes,
            },
        )
        return self.client.build_authorization_url(
            redirect_uri=self.redirect_uri,
            state=state,
            scopes=merged_scopes,
        )

    def handle_callback(self, query: dict[str, str]) -> OAuthCallbackResult:
        state = str(query.get("state") or "").strip()
        code = str(query.get("code") or "").strip()
        oauth_error = str(query.get("error") or "").strip()
        oauth_error_description = str(query.get("error_description") or "").strip()

        if not state:
            return OAuthCallbackResult(False, 400, "Missing state parameter.")

        if oauth_error:
            self.store.finalize_oauth_state(state, status="failed", last_error=oauth_error_description or oauth_error)
            self.store.record_event_audit(
                "oauth_callback_failed",
                payload={
                    "provider": "feishu",
                    "state": state,
                    "error": oauth_error,
                    "error_description": oauth_error_description,
                },
            )
            return OAuthCallbackResult(False, 400, oauth_error_description or oauth_error)

        claimed = self.store.claim_oauth_state(state, now_iso=self._now_iso())
        if claimed is None:
            return OAuthCallbackResult(False, 400, "Invalid, expired, or already used state.")

        if not code:
            self.store.finalize_oauth_state(state, status="failed", last_error="missing authorization code")
            return OAuthCallbackResult(False, 400, "Missing code parameter.")

        try:
            token_payload = self.client.exchange_code(code=code, redirect_uri=self.redirect_uri)
            access_token = str(token_payload.get("access_token") or "").strip()
            refresh_token = str(token_payload.get("refresh_token") or "").strip()
            if not access_token:
                raise FeishuOAuthError("Feishu OAuth response missing access_token")

            user_info = self.client.get_user_info(access_token=access_token)
            open_id = (
                str(user_info.get("open_id") or "").strip()
                or str((user_info.get("user") or {}).get("open_id") or "").strip()
            )
            if not open_id:
                raise FeishuOAuthError("Feishu user info response missing open_id")

            expires_in = int(token_payload.get("expires_in") or 0)
            refresh_expires_in = int(token_payload.get("refresh_expires_in") or 0)
            now = datetime.now()
            expires_at = (now + timedelta(seconds=max(0, expires_in))).isoformat()
            refresh_expires_at = (
                now + timedelta(seconds=max(0, refresh_expires_in))
            ).isoformat() if refresh_expires_in > 0 else None

            self.store.upsert_feishu_user_token(
                open_id,
                app_id=self.client.app_id,
                access_token=access_token,
                refresh_token=refresh_token,
                token_type=str(token_payload.get("token_type") or "Bearer"),
                scope=str(token_payload.get("scope") or ""),
                expires_at=expires_at,
                refresh_expires_at=refresh_expires_at,
                status="active",
                last_refreshed_at=now.isoformat(),
                last_error=None,
                payload=token_payload,
            )
            self.store.finalize_oauth_state(state, status="consumed", last_error=None)
            self.store.record_event_audit(
                "oauth_callback_succeeded",
                chat_id=str(claimed.get("chat_id") or "") or None,
                payload={
                    "provider": "feishu",
                    "state": state,
                    "open_id": open_id,
                },
            )
            return OAuthCallbackResult(
                True,
                200,
                "Authorization completed. You can return to Feishu now.",
                open_id=open_id,
                chat_id=str(claimed.get("chat_id") or "") or None,
                thread_id=str(claimed.get("thread_id") or "") or None,
            )
        except Exception as exc:
            error_text = str(exc)
            self.store.finalize_oauth_state(state, status="failed", last_error=error_text)
            self.store.record_event_audit(
                "oauth_callback_failed",
                chat_id=str(claimed.get("chat_id") or "") or None,
                payload={
                    "provider": "feishu",
                    "state": state,
                    "error": error_text,
                },
            )
            if isinstance(exc, FeishuOAuthError):
                return OAuthCallbackResult(False, 400, error_text)
            return OAuthCallbackResult(False, 500, "OAuth callback handling failed.")

    def get_user_token_status(self, open_id: str) -> str:
        row = self.store.get_feishu_user_token(open_id)
        if not row:
            return "not_connected"
        return str(row.get("status") or "active")


class FeishuUserTokenManager:
    """Returns valid user access token with automatic refresh and persistence."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        client: FeishuOAuthClient,
        refresh_ahead_seconds: int = 300,
    ):
        self.store = store
        self.client = client
        self.refresh_ahead_seconds = max(0, int(refresh_ahead_seconds))
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()

    def _get_lock(self, open_id: str) -> threading.Lock:
        with self._global_lock:
            lock = self._locks.get(open_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[open_id] = lock
            return lock

    @staticmethod
    def _parse_iso(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def get_valid_access_token(self, open_id: str) -> str:
        lock = self._get_lock(open_id)
        with lock:
            row = self.store.get_feishu_user_token(open_id)
            if row is None:
                raise FeishuReauthorizationRequired("No Feishu OAuth token for this user")

            status = str(row.get("status") or "active")
            if status in {"reauth_required", "revoked"}:
                raise FeishuReauthorizationRequired("Feishu OAuth requires re-authorization")

            access_token = str(row.get("access_token") or "").strip()
            refresh_token = str(row.get("refresh_token") or "").strip()
            expires_at = self._parse_iso(str(row.get("expires_at") or ""))
            now = datetime.now()

            if access_token and expires_at is not None and now < (expires_at - timedelta(seconds=self.refresh_ahead_seconds)):
                return access_token

            if not refresh_token:
                self.store.update_feishu_user_token_status(
                    open_id,
                    status="reauth_required",
                    last_error="missing refresh token",
                )
                raise FeishuReauthorizationRequired("Missing refresh token")

            try:
                refreshed = self.client.refresh_access_token(refresh_token=refresh_token)
                refreshed_access = str(refreshed.get("access_token") or "").strip()
                refreshed_refresh = str(refreshed.get("refresh_token") or refresh_token).strip()
                if not refreshed_access:
                    raise FeishuOAuthError("refresh response missing access_token")

                refreshed_expires_in = int(refreshed.get("expires_in") or 0)
                refreshed_refresh_expires_in = int(refreshed.get("refresh_expires_in") or 0)
                refreshed_scope = str(refreshed.get("scope") or row.get("scope") or "")
                refreshed_token_type = str(refreshed.get("token_type") or row.get("token_type") or "Bearer")
                refreshed_now = datetime.now()

                self.store.upsert_feishu_user_token(
                    open_id,
                    app_id=str(row.get("app_id") or self.client.app_id),
                    access_token=refreshed_access,
                    refresh_token=refreshed_refresh,
                    token_type=refreshed_token_type,
                    scope=refreshed_scope,
                    expires_at=(
                        refreshed_now + timedelta(seconds=max(0, refreshed_expires_in))
                    ).isoformat(),
                    refresh_expires_at=(
                        refreshed_now + timedelta(seconds=max(0, refreshed_refresh_expires_in))
                    ).isoformat() if refreshed_refresh_expires_in > 0 else str(row.get("refresh_expires_at") or "") or None,
                    status="active",
                    last_refreshed_at=refreshed_now.isoformat(),
                    last_error=None,
                    payload=refreshed,
                )
                self.store.record_event_audit(
                    "oauth_token_refreshed",
                    payload={"provider": "feishu", "open_id": open_id},
                )
                return refreshed_access
            except Exception as exc:
                err = str(exc)
                token_still_usable = access_token and expires_at is not None and now < expires_at
                if token_still_usable:
                    self.store.update_feishu_user_token_status(
                        open_id,
                        status="refresh_failed",
                        last_error=err,
                    )
                    self.store.record_event_audit(
                        "oauth_token_refresh_failed",
                        payload={"provider": "feishu", "open_id": open_id, "degraded": True, "error": err},
                    )
                    return access_token

                self.store.update_feishu_user_token_status(
                    open_id,
                    status="reauth_required",
                    last_error=err,
                )
                self.store.record_event_audit(
                    "oauth_token_refresh_failed",
                    payload={"provider": "feishu", "open_id": open_id, "degraded": False, "error": err},
                )
                raise FeishuReauthorizationRequired("Feishu OAuth token expired; re-authorization required")
