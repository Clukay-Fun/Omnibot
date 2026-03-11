from __future__ import annotations

from pathlib import Path

from nanobot.feishu.persona import FeishuPersonaOverlayManager


def test_persona_manager_creates_dm_overlay_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "USER.md").write_text("global user", encoding="utf-8")
    (workspace / "BOOTSTRAP.md").write_text("bootstrap", encoding="utf-8")

    manager = FeishuPersonaOverlayManager(workspace)
    overlay = manager.ensure_dm_overlay("tenant-1", "ou_user_1")

    assert overlay == workspace / "users" / "feishu" / "tenant-1" / "ou_user_1"
    assert (overlay / "USER.md").read_text(encoding="utf-8") == "global user"
    assert (overlay / "BOOTSTRAP.md").read_text(encoding="utf-8") == "bootstrap"


def test_persona_manager_skips_group_overlay(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = FeishuPersonaOverlayManager(workspace)

    assert manager.overlay_root_for_chat("group", "tenant-1", "ou_user_1") is None


def test_persona_manager_refreshes_placeholder_overlay_from_global_user(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "USER.md").write_text("- **昵称**：康哥\n", encoding="utf-8")
    (workspace / "BOOTSTRAP.md").write_text("bootstrap", encoding="utf-8")

    manager = FeishuPersonaOverlayManager(workspace)
    overlay = manager.ensure_dm_overlay("tenant-1", "ou_user_1")
    (overlay / "USER.md").write_text("- **昵称**：(你的名字)\n", encoding="utf-8")

    overlay_again = manager.ensure_dm_overlay("tenant-1", "ou_user_1")

    assert overlay_again == overlay
    assert (overlay / "USER.md").read_text(encoding="utf-8") == "- **昵称**：康哥\n"


def test_persona_manager_disables_bootstrap_for_personalized_overlay(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "USER.md").write_text("- **昵称**：康哥\n", encoding="utf-8")
    (workspace / "BOOTSTRAP.md").write_text("bootstrap", encoding="utf-8")

    manager = FeishuPersonaOverlayManager(workspace)
    overlay = manager.ensure_dm_overlay("tenant-1", "ou_user_1")

    assert manager.should_include_bootstrap(overlay) is False
