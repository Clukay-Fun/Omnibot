"""Per-user Feishu private-chat workspaces stored under the global workspace."""

from __future__ import annotations

import json
from datetime import datetime
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import Any

from nanobot.agent.overlay import OverlayContext
from nanobot.utils.helpers import ensure_dir, safe_filename


class FeishuUserWorkspaceManager:
    """Manage per-user Feishu DM workspaces, migration, and heartbeat targets."""

    _USER_PLACEHOLDER_MARKERS = (
        "(你的名字)",
        "(你希望我如何称呼你)",
        "(你的时区，例如：UTC+8)",
        "(偏好的语言)",
        "(待了解)",
    )
    _GLOBAL_RESET_FILES = (
        "USER.md",
        "BOOTSTRAP.md",
        "HEARTBEAT.md",
        "memory/MEMORY.md",
        "memory/HISTORY.md",
    )
    _PER_USER_FILES = (
        "USER.md",
        "BOOTSTRAP.md",
        "HEARTBEAT.md",
        "memory/MEMORY.md",
        "memory/HISTORY.md",
    )

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.root = ensure_dir(workspace / "users" / "feishu")
        self.migration_marker = self.root / ".global_user_migration.json"

    def overlay_root_for_chat(
        self,
        chat_type: str,
        tenant_key: str,
        user_open_id: str,
    ) -> Path | None:
        """Return the per-user workspace root for a Feishu DM."""
        if chat_type == "group" or not tenant_key or not user_open_id:
            return None
        return self.ensure_dm_workspace(tenant_key, user_open_id)

    def ensure_dm_workspace(self, tenant_key: str, user_open_id: str) -> Path:
        """Create or update the per-user DM workspace without overwriting user edits."""
        tenant_dir = safe_filename(tenant_key) or "tenant"
        user_dir = safe_filename(user_open_id) or "user"
        workspace_root = ensure_dir(self.root / tenant_dir / user_dir)

        self._maybe_migrate_global_user_state(
            target_root=workspace_root,
            tenant_key=tenant_key,
            user_open_id=user_open_id,
        )
        self._ensure_per_user_files(workspace_root)
        return workspace_root

    def should_include_bootstrap(self, overlay_root: Path) -> bool:
        """BOOTSTRAP.md stays active until USER.md is no longer placeholder-like."""
        user_file = overlay_root / "USER.md"
        return self._is_placeholder_user_text(user_file.read_text(encoding="utf-8")) if user_file.exists() else True

    def list_heartbeat_targets(self) -> list["HeartbeatTarget"]:
        """Enumerate Feishu DM heartbeat targets from per-user workspaces."""
        from nanobot.heartbeat.service import HeartbeatTarget

        targets: list[HeartbeatTarget] = []
        for heartbeat_file in sorted(self.root.glob("*/*/HEARTBEAT.md")):
            workspace_root = heartbeat_file.parent
            user_open_id = workspace_root.name
            targets.append(
                HeartbeatTarget(
                    workspace_root=workspace_root,
                    channel="feishu",
                    chat_id=user_open_id,
                    session_key=f"feishu:dm:{user_open_id}:heartbeat",
                    overlay_context=OverlayContext(
                        system_overlay_root=str(workspace_root),
                        system_overlay_bootstrap=self.should_include_bootstrap(workspace_root),
                    ),
                )
            )
        return targets

    def _ensure_per_user_files(self, workspace_root: Path) -> None:
        for relative_path in self._PER_USER_FILES:
            destination = workspace_root / relative_path
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(self._read_template_text(relative_path, per_user=True), encoding="utf-8")

    def _maybe_migrate_global_user_state(
        self,
        *,
        target_root: Path,
        tenant_key: str,
        user_open_id: str,
    ) -> None:
        if self.migration_marker.exists():
            return

        if not self._global_user_state_is_meaningful():
            self._write_migration_marker(
                status="skipped_template_like",
                tenant_key=tenant_key,
                user_open_id=user_open_id,
                target_root=target_root,
                snapshot_dir=None,
            )
            return

        snapshot_dir = self._snapshot_global_user_state()
        self._seed_target_from_global_state(target_root)
        self._reset_global_user_state()
        self._write_migration_marker(
            status="migrated",
            tenant_key=tenant_key,
            user_open_id=user_open_id,
            target_root=target_root,
            snapshot_dir=snapshot_dir,
        )

    def _global_user_state_is_meaningful(self) -> bool:
        user_path = self.workspace / "USER.md"
        if user_path.exists() and not self._texts_equivalent(
            user_path.read_text(encoding="utf-8"),
            self._read_template_text("USER.md"),
        ):
            return True

        heartbeat_path = self.workspace / "HEARTBEAT.md"
        if heartbeat_path.exists():
            heartbeat_text = heartbeat_path.read_text(encoding="utf-8")
            if not self._texts_equivalent(heartbeat_text, self._read_template_text("HEARTBEAT.md")):
                stripped = heartbeat_text.strip()
                if stripped and not self._heartbeat_has_only_headings_and_comments(stripped):
                    return True

        memory_path = self.workspace / "memory" / "MEMORY.md"
        if memory_path.exists() and not self._texts_equivalent(
            memory_path.read_text(encoding="utf-8"),
            self._read_template_text("memory/MEMORY.md"),
        ):
            return True

        history_path = self.workspace / "memory" / "HISTORY.md"
        if history_path.exists() and history_path.read_text(encoding="utf-8").strip():
            return True

        return False

    def _seed_target_from_global_state(self, target_root: Path) -> None:
        for relative_path in self._GLOBAL_RESET_FILES:
            src = self.workspace / relative_path
            if not src.exists():
                continue
            dest = target_root / relative_path
            template_text = self._read_template_text(relative_path, per_user=True)
            if dest.exists() and not self._texts_equivalent(dest.read_text(encoding="utf-8"), template_text):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    def _snapshot_global_user_state(self) -> Path:
        snapshot_dir = ensure_dir(
            self.workspace / "legacy_global_seed" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        )
        for relative_path in self._GLOBAL_RESET_FILES:
            src = self.workspace / relative_path
            if not src.exists():
                continue
            dest = snapshot_dir / relative_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        return snapshot_dir

    def _reset_global_user_state(self) -> None:
        for relative_path in self._GLOBAL_RESET_FILES:
            dest = self.workspace / relative_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(self._read_template_text(relative_path, per_user=False), encoding="utf-8")

    def _write_migration_marker(
        self,
        *,
        status: str,
        tenant_key: str,
        user_open_id: str,
        target_root: Path,
        snapshot_dir: Path | None,
    ) -> None:
        payload: dict[str, Any] = {
            "status": status,
            "migrated_at": datetime.now().isoformat(),
            "tenant_key": tenant_key,
            "user_open_id": user_open_id,
            "target_root": str(target_root),
            "snapshot_dir": str(snapshot_dir) if snapshot_dir is not None else None,
        }
        self.migration_marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _read_template_text(self, relative_path: str, per_user: bool = False) -> str:
        template_root = pkg_files("nanobot") / "templates"
        if per_user:
            template_root = template_root / "per_user"
        for segment in relative_path.split("/"):
            template_root = template_root / segment
        return template_root.read_text(encoding="utf-8") if template_root.exists() else ""

    def _heartbeat_has_only_headings_and_comments(self, text: str) -> bool:
        stripped_lines = []
        for line in text.splitlines():
            raw = line.strip()
            if not raw:
                continue
            if raw.startswith("<!--") and raw.endswith("-->"):
                continue
            if raw.startswith("#"):
                continue
            stripped_lines.append(raw)
        return not stripped_lines

    def _is_placeholder_user_text(self, text: str) -> bool:
        return any(marker in text for marker in self._USER_PLACEHOLDER_MARKERS)

    @staticmethod
    def _normalize_text(text: str) -> str:
        return text.replace("\r\n", "\n").strip()

    def _texts_equivalent(self, left: str, right: str) -> bool:
        return self._normalize_text(left) == self._normalize_text(right)


# Backward-compatible alias for callers/tests that still import the old name.
FeishuPersonaOverlayManager = FeishuUserWorkspaceManager
