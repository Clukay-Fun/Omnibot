from __future__ import annotations

import json
from pathlib import Path

from nanobot.feishu.persona import FeishuUserWorkspaceManager


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "memory").mkdir()
    return workspace


def test_workspace_manager_creates_dm_workspace_from_per_user_templates(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    manager = FeishuUserWorkspaceManager(workspace)

    overlay = manager.ensure_dm_workspace("tenant-1", "ou_user_1")

    assert overlay == workspace / "users" / "feishu" / "tenant-1" / "ou_user_1"
    assert (overlay / "USER.md").exists()
    assert (overlay / "BOOTSTRAP.md").exists()
    assert (overlay / "HEARTBEAT.md").exists()
    assert (overlay / "memory" / "MEMORY.md").exists()
    assert (overlay / "memory" / "HISTORY.md").exists()
    assert "(待了解)" in (overlay / "USER.md").read_text(encoding="utf-8")
    assert "低打扰维护规则" in (overlay / "HEARTBEAT.md").read_text(encoding="utf-8")
    assert manager.should_include_bootstrap(overlay) is True


def test_workspace_manager_skips_group_overlay(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    manager = FeishuUserWorkspaceManager(workspace)

    assert manager.overlay_root_for_chat("group", "tenant-1", "ou_user_1") is None


def test_workspace_manager_does_not_reinitialize_existing_files_but_backfills_missing(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    manager = FeishuUserWorkspaceManager(workspace)

    overlay = manager.ensure_dm_workspace("tenant-1", "ou_user_1")
    user_file = overlay / "USER.md"
    heartbeat_file = overlay / "HEARTBEAT.md"
    user_file.write_text("- **昵称**：康哥\n", encoding="utf-8")
    heartbeat_file.unlink()

    overlay_again = manager.ensure_dm_workspace("tenant-1", "ou_user_1")

    assert overlay_again == overlay
    assert user_file.read_text(encoding="utf-8") == "- **昵称**：康哥\n"
    assert heartbeat_file.exists()


def test_workspace_manager_skips_template_like_global_migration(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    manager = FeishuUserWorkspaceManager(workspace)

    overlay = manager.ensure_dm_workspace("tenant-1", "ou_user_1")
    marker = json.loads((workspace / "users" / "feishu" / ".global_user_migration.json").read_text(encoding="utf-8"))

    assert marker["status"] == "skipped_template_like"
    assert marker["tenant_key"] == "tenant-1"
    assert marker["user_open_id"] == "ou_user_1"
    assert marker["snapshot_dir"] is None
    assert "(待了解)" in (overlay / "USER.md").read_text(encoding="utf-8")


def test_workspace_manager_migrates_meaningful_global_state_once_and_resets_root(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    (workspace / "USER.md").write_text("- **昵称**：康哥\n", encoding="utf-8")
    (workspace / "BOOTSTRAP.md").write_text("legacy bootstrap", encoding="utf-8")
    (workspace / "HEARTBEAT.md").write_text("- [ ] Follow up with 康哥\n", encoding="utf-8")
    (workspace / "memory" / "MEMORY.md").write_text("Known preference: concise", encoding="utf-8")
    (workspace / "memory" / "HISTORY.md").write_text("[2026-03-11 10:00] promised a follow-up", encoding="utf-8")

    manager = FeishuUserWorkspaceManager(workspace)
    overlay = manager.ensure_dm_workspace("tenant-1", "ou_user_1")

    assert (overlay / "USER.md").read_text(encoding="utf-8") == "- **昵称**：康哥\n"
    assert (overlay / "BOOTSTRAP.md").read_text(encoding="utf-8") == "legacy bootstrap"
    assert (overlay / "HEARTBEAT.md").read_text(encoding="utf-8") == "- [ ] Follow up with 康哥\n"
    assert (overlay / "memory" / "MEMORY.md").read_text(encoding="utf-8") == "Known preference: concise"
    assert (overlay / "memory" / "HISTORY.md").read_text(encoding="utf-8") == "[2026-03-11 10:00] promised a follow-up"

    marker = json.loads((workspace / "users" / "feishu" / ".global_user_migration.json").read_text(encoding="utf-8"))
    assert marker["status"] == "migrated"
    assert marker["tenant_key"] == "tenant-1"
    assert marker["user_open_id"] == "ou_user_1"
    assert Path(marker["snapshot_dir"]).exists()

    assert "(你的名字)" in (workspace / "USER.md").read_text(encoding="utf-8")
    assert "Hello, World" in (workspace / "BOOTSTRAP.md").read_text(encoding="utf-8")
    assert "活动检查任务" in (workspace / "HEARTBEAT.md").read_text(encoding="utf-8")
    assert "# Long-term Memory" in (workspace / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert (workspace / "memory" / "HISTORY.md").read_text(encoding="utf-8") == ""

    second_overlay = manager.ensure_dm_workspace("tenant-1", "ou_user_2")
    assert "(待了解)" in (second_overlay / "USER.md").read_text(encoding="utf-8")
    assert "康哥" not in (second_overlay / "USER.md").read_text(encoding="utf-8")


def test_workspace_manager_lists_feishu_heartbeat_targets(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    manager = FeishuUserWorkspaceManager(workspace)

    overlay = manager.ensure_dm_workspace("tenant-1", "ou_user_1")
    (overlay / "USER.md").write_text("- **昵称**：康哥\n", encoding="utf-8")

    targets = manager.list_heartbeat_targets()

    assert len(targets) == 1
    target = targets[0]
    assert target.workspace_root == overlay
    assert target.channel == "feishu"
    assert target.chat_id == "ou_user_1"
    assert target.session_key == "feishu:dm:ou_user_1:heartbeat"
    assert target.overlay_context is not None
    assert target.overlay_context.system_overlay_root == str(overlay)
    assert target.overlay_context.system_overlay_bootstrap is False
