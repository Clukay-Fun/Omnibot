"""Webhook validation helpers for Feishu events."""

from __future__ import annotations

from typing import Any


class FeishuWebhookSecurity:
    """Validate Feishu webhook payloads with the configured verification token."""

    def __init__(self, verification_token: str = ""):
        self.verification_token = verification_token

    def is_valid(self, payload: dict[str, Any]) -> bool:
        if not self.verification_token:
            return True
        token = payload.get("token")
        if token is None and isinstance(payload.get("header"), dict):
            token = payload["header"].get("token")
        return token == self.verification_token

    @staticmethod
    def build_challenge_response(payload: dict[str, Any]) -> dict[str, str] | None:
        if payload.get("type") != "url_verification":
            return None
        challenge = payload.get("challenge")
        if not challenge:
            return None
        return {"challenge": challenge}
