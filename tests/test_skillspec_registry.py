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
        assert registry.report.source_collisions == ["case_search: builtin:case_search.yaml -> workspace:case_search.yaml"]
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


def test_registry_applies_three_level_precedence_with_managed_layer() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        builtin = root / "builtin"
        workspace = root / "workspace"
        managed = workspace / "managed"

        _write_yaml(
            builtin / "task_search.yaml",
            """
            meta:
              id: task_search
              version: "0.1"
              enabled: true
              description: builtin
            action:
              kind: query
            response: {}
            error: {}
            """,
        )
        _write_yaml(
            managed / "task_search.yaml",
            """
            meta:
              id: task_search
              version: "0.1"
              enabled: true
              description: managed
            action:
              kind: query
            response: {}
            error: {}
            """,
        )
        _write_yaml(
            workspace / "task_search.yaml",
            """
            meta:
              id: task_search
              version: "0.1"
              enabled: true
              description: workspace
            action:
              kind: query
            response: {}
            error: {}
            """,
        )

        registry = SkillSpecRegistry(workspace_root=workspace, builtin_root=builtin)
        specs = registry.load()

        assert specs["task_search"].meta.description == "workspace"
        assert registry.report.overridden == ["task_search"]
        assert registry.report.source_collisions == [
            "task_search: builtin:task_search.yaml -> managed:task_search.yaml",
            "task_search: managed:task_search.yaml -> workspace:task_search.yaml",
        ]


def test_registry_managed_layer_can_disable_builtin_when_workspace_missing() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        builtin = root / "builtin"
        workspace = root / "workspace"
        managed = workspace / "managed"

        _write_yaml(
            builtin / "deadline_overview.yaml",
            """
            meta:
              id: deadline_overview
              version: "0.1"
              enabled: true
            action:
              kind: query
            response: {}
            error: {}
            """,
        )
        _write_yaml(
            managed / "deadline_overview.yaml",
            """
            meta:
              id: deadline_overview
              version: "0.1"
              enabled: false
            action:
              kind: query
            response: {}
            error: {}
            """,
        )

        registry = SkillSpecRegistry(workspace_root=workspace, builtin_root=builtin)
        specs = registry.load()

        assert "deadline_overview" not in specs
        assert registry.report.overridden == ["deadline_overview"]
        assert registry.report.disabled == ["deadline_overview"]
