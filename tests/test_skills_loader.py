from pathlib import Path

from nanobot.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader


def test_skills_loader_discovers_builtins_from_builtin_subdir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    builtin_root = tmp_path / "builtin"
    builtin_skill = builtin_root / "builtin" / "memory" / "SKILL.md"
    builtin_skill.parent.mkdir(parents=True, exist_ok=True)
    builtin_skill.write_text("---\nname: memory\ndescription: Builtin memory skill.\n---\n\n# Memory\n", encoding="utf-8")

    loader = SkillsLoader(workspace=workspace, builtin_skills_dir=builtin_root / "builtin")

    assert loader.load_skill("memory") == builtin_skill.read_text(encoding="utf-8")
    assert loader.list_skills(filter_unavailable=False) == [
        {"name": "memory", "path": str(builtin_skill), "source": "builtin"}
    ]


def test_skills_loader_workspace_overlay_keeps_precedence_over_builtin(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace_skill = workspace / "skills" / "memory" / "SKILL.md"
    builtin_skill = tmp_path / "builtin" / "memory" / "SKILL.md"
    workspace_skill.parent.mkdir(parents=True, exist_ok=True)
    builtin_skill.parent.mkdir(parents=True, exist_ok=True)
    workspace_skill.write_text("# Workspace Memory\n", encoding="utf-8")
    builtin_skill.write_text("# Builtin Memory\n", encoding="utf-8")

    loader = SkillsLoader(workspace=workspace, builtin_skills_dir=tmp_path / "builtin")

    assert loader.load_skill("memory") == "# Workspace Memory\n"
    assert loader.list_skills(filter_unavailable=False) == [
        {"name": "memory", "path": str(workspace_skill), "source": "workspace"}
    ]


def test_skills_loader_default_builtin_tree_exposes_packaged_memory_skill(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    loader = SkillsLoader(workspace=workspace)

    listed = loader.list_skills(filter_unavailable=False)
    assert any(item["name"] == "memory" and item["path"] == str(BUILTIN_SKILLS_DIR / "memory" / "SKILL.md") for item in listed)

    loaded = loader.load_skill("memory")
    assert loaded is not None
    assert "Memory" in loaded
