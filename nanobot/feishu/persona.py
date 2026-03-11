"""Per-user Feishu persona overlays stored under the workspace."""

from __future__ import annotations

from pathlib import Path

from nanobot.utils.helpers import ensure_dir, safe_filename


class FeishuPersonaOverlayManager:
    """Manage per-user DM overlay files for Feishu onboarding and persona."""

    _USER_PLACEHOLDER_MARKERS = (
        "(你的名字)",
        "(你希望我如何称呼你)",
        "(你的时区，例如：UTC+8)",
        "(偏好的语言)",
    )

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.root = ensure_dir(workspace / "users" / "feishu")

    def overlay_root_for_chat(
        self,
        chat_type: str,
        tenant_key: str,
        user_open_id: str,
    ) -> Path | None:
        if chat_type == "group" or not tenant_key or not user_open_id:
            return None
        return self.ensure_dm_overlay(tenant_key, user_open_id)

    def ensure_dm_overlay(self, tenant_key: str, user_open_id: str) -> Path:
        tenant_dir = safe_filename(tenant_key) or "tenant"
        user_dir = safe_filename(user_open_id) or "user"
        overlay = ensure_dir(self.root / tenant_dir / user_dir)
        self._copy_if_missing("USER.md", overlay / "USER.md")
        self._copy_if_missing("BOOTSTRAP.md", overlay / "BOOTSTRAP.md")
        self._refresh_user_overlay_if_stale(overlay / "USER.md")
        return overlay

    def should_include_bootstrap(self, overlay_root: Path) -> bool:
        user_file = overlay_root / "USER.md"
        return self._is_placeholder_user_text(user_file.read_text(encoding="utf-8")) if user_file.exists() else True

    def _copy_if_missing(self, filename: str, dest: Path) -> None:
        if dest.exists():
            return
        src = self.workspace / filename
        if src.exists():
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    def _refresh_user_overlay_if_stale(self, overlay_user: Path) -> None:
        global_user = self.workspace / "USER.md"
        if not overlay_user.exists() or not global_user.exists():
            return
        overlay_text = overlay_user.read_text(encoding="utf-8")
        global_text = global_user.read_text(encoding="utf-8")
        if self._is_placeholder_user_text(overlay_text) and not self._is_placeholder_user_text(global_text):
            overlay_user.write_text(global_text, encoding="utf-8")

    def _is_placeholder_user_text(self, text: str) -> bool:
        return any(marker in text for marker in self._USER_PLACEHOLDER_MARKERS)
