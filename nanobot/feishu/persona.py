"""Per-user Feishu private-chat workspaces stored under the global workspace."""

from __future__ import annotations

import json
from datetime import datetime
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nanobot.heartbeat.service import HeartbeatTarget

from nanobot.agent.overlay import OverlayContext
from nanobot.utils.helpers import ensure_dir, safe_filename


class FeishuUserWorkspaceManager:
    """Manage per-user Feishu DM workspaces, migration, and heartbeat targets."""

    _PLACEHOLDER_VALUES = (
        "(你的名字)",
        "(你希望我如何称呼你)",
        "(你的时区，例如：UTC+8)",
        "(偏好的语言)",
        "(你的角色，例如：开发者、研究员)",
        "(长期稳定的工作方向或项目背景)",
        "(IDE、编程语言、框架)",
        "(例如：直接、温和、结论先行)",
        "(例如：先给全局，再拆下一步)",
        "(例如：宁可多确认，也不要误判)",
        "(例如：飞书、终端、文档、脚本)",
        "(例如：过度打扰、过早展开、模糊建议)",
        "(待了解)",
    )
    _GLOBAL_RESET_FILES = (
        "USER.md",
        "BOOTSTRAP.md",
        "HEARTBEAT.md",
        "WORKLOG.md",
        "memory/MEMORY.md",
        "memory/HISTORY.md",
    )
    _PER_USER_FILES = (
        "USER.md",
        "BOOTSTRAP.md",
        "HEARTBEAT.md",
        "WORKLOG.md",
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
        """BOOTSTRAP.md stays active until key onboarding facts are sufficiently filled in."""
        user_file = overlay_root / "USER.md"
        worklog_file = overlay_root / "WORKLOG.md"
        memory_file = overlay_root / "memory" / "MEMORY.md"
        if not user_file.exists():
            return True
        return not self._has_bootstrap_minimum_context(
            user_text=user_file.read_text(encoding="utf-8"),
            worklog_text=worklog_file.read_text(encoding="utf-8") if worklog_file.exists() else "",
            memory_text=memory_file.read_text(encoding="utf-8") if memory_file.exists() else "",
        )

    def list_heartbeat_targets(self) -> list[HeartbeatTarget]:
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

        worklog_path = self.workspace / "WORKLOG.md"
        if worklog_path.exists() and not self._texts_equivalent(
            worklog_path.read_text(encoding="utf-8"),
            self._read_template_text("WORKLOG.md"),
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

    def _has_bootstrap_minimum_context(self, *, user_text: str, worklog_text: str, memory_text: str) -> bool:
        return (
            self._user_has_name(user_text)
            and self._user_has_style(user_text)
            and self._user_has_long_term_context(user_text, memory_text)
            and self._worklog_has_active_item(worklog_text)
        )

    def _user_has_name(self, text: str) -> bool:
        nickname = self._extract_field(text, "昵称")
        title = self._extract_field(text, "称呼方式")
        return self._is_meaningful_value(nickname) or self._is_meaningful_value(title)

    def _user_has_style(self, text: str) -> bool:
        return (
            self._has_checked_option(text, "### 回复长度", ("简短且简洁", "详细的解释", "根据问题自适应"))
            or self._has_checked_option(text, "### 沟通风格", ("随意的", "专业的", "技术的"))
            or self._is_meaningful_value(self._extract_field(text, "表达风格偏好"))
        )

    def _user_has_long_term_context(self, user_text: str, memory_text: str) -> bool:
        return self._is_meaningful_value(self._extract_field(user_text, "长期工作背景")) or self._memory_has_meaningful_content(
            memory_text
        )

    @staticmethod
    def _worklog_has_active_item(text: str) -> bool:
        in_completed = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("## "):
                in_completed = line == "## 已完成"
                continue
            if line.startswith("### ") and not in_completed:
                return True
        return False

    def _memory_has_meaningful_content(self, text: str) -> bool:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#"):
                continue
            if line.startswith("（") and line.endswith("）"):
                continue
            if line.startswith("*") and line.endswith("*"):
                continue
            return True
        return False

    @staticmethod
    def _extract_field(text: str, field_name: str) -> str:
        prefix = f"- **{field_name}**："
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith(prefix):
                return line[len(prefix):].strip()
        return ""

    @staticmethod
    def _has_checked_option(text: str, section_heading: str, labels: tuple[str, ...]) -> bool:
        in_section = False
        allowed = set(labels)
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("### "):
                in_section = line == section_heading
                continue
            if not in_section or not line.startswith("- [x] "):
                continue
            label = line.removeprefix("- [x] ").strip()
            if label in allowed:
                return True
        return False

    def _is_meaningful_value(self, value: str) -> bool:
        normalized = value.strip()
        return bool(normalized) and normalized not in self._PLACEHOLDER_VALUES

    @staticmethod
    def _normalize_text(text: str) -> str:
        return text.replace("\r\n", "\n").strip()

    def _texts_equivalent(self, left: str, right: str) -> bool:
        return self._normalize_text(left) == self._normalize_text(right)


# Backward-compatible alias for callers/tests that still import the old name.
FeishuPersonaOverlayManager = FeishuUserWorkspaceManager
