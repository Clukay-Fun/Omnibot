from pathlib import Path
from textwrap import dedent

from nanobot.agent.skill_runtime.registry import SkillSpecRegistry


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content).strip() + "\n", encoding="utf-8")


def test_registry_applies_workspace_override_and_disabled() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        builtin = root / "builtin"
        workspace = root / "workspace"

        _write_yaml(
            builtin / "case_search.yaml",
            """
            meta:
              id: case_search
              version: "0.1"
              enabled: true
            action:
              kind: tool
            response: {}
            error: {}
            """,
        )
        _write_yaml(
            workspace / "case_search.yaml",
            """
            meta:
              id: case_search
              version: "0.1"
              enabled: false
            action:
              kind: tool
            response: {}
            error: {}
            """,
        )

        registry = SkillSpecRegistry(workspace_root=workspace, builtin_root=builtin)
        specs = registry.load()

        assert specs == {}
        assert registry.report.loaded == []
        assert registry.report.overridden == ["case_search"]
        assert registry.report.disabled == ["case_search"]


def test_registry_reports_invalid_yaml() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "broken.yaml").write_text("meta: [\n", encoding="utf-8")

        registry = SkillSpecRegistry(workspace_root=workspace, builtin_root=root / "builtin")
        registry.load()

        assert registry.report.loaded == []
        assert len(registry.report.invalid) == 1
        assert "workspace:broken" in registry.report.invalid[0]
