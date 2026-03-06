from pathlib import Path
from textwrap import dedent

from nanobot.agent.skill_runtime.registry import SkillSpecRegistry
from nanobot.agent.skill_runtime.table_registry import TableRegistry


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
        assert len(registry.report.source_collisions) == 1
        collision = registry.report.source_collisions[0]
        assert collision.startswith("case_search: bundled:")
        assert " -> workspace:" in collision
        assert "case_search.yaml" in collision
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
        assert len(registry.report.source_collisions) == 2
        assert registry.report.source_collisions[0].startswith("task_search: bundled:")
        assert " -> managed:" in registry.report.source_collisions[0]
        assert " -> workspace:" in registry.report.source_collisions[1]


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


def test_registry_merges_by_meta_id_even_when_filename_differs() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        builtin = root / "builtin"
        workspace = root / "workspace"

        _write_yaml(
            builtin / "legacy_name.yaml",
            """
            meta:
              id: customer_search
              version: "0.1"
              enabled: true
              description: bundled
            action:
              kind: query
            response: {}
            error: {}
            """,
        )
        _write_yaml(
            workspace / "new_name.yaml",
            """
            meta:
              id: customer_search
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

        assert specs["customer_search"].meta.description == "workspace"
        assert registry.report.overridden == ["customer_search"]
        assert len(registry.report.source_collisions) == 1
        assert "legacy_name.yaml" in registry.report.source_collisions[0]
        assert "new_name.yaml" in registry.report.source_collisions[0]


def test_registry_skips_internal_underscore_prefixed_files() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / "workspace"

        _write_yaml(
            workspace / "_index.yaml",
            """
            skills:
              - case_search
            """,
        )
        _write_yaml(
            workspace / "case_search.yaml",
            """
            meta:
              id: case_search
              version: "0.1"
              enabled: true
            action:
              kind: query
            response: {}
            error: {}
            """,
        )

        registry = SkillSpecRegistry(workspace_root=workspace, builtin_root=root / "builtin")
        specs = registry.load()

        assert "case_search" in specs
        assert all("_index" not in item for item in registry.report.invalid)


def test_table_registry_merges_workspace_over_builtin(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin.yaml"
    _write_yaml(
        builtin,
        """
        version: 1
        tables:
          case_registry:
            app_token: app_builtin
            table_id: tbl_builtin
            field_aliases:
              title: 案号
        """,
    )

    workspace = tmp_path / "workspace"
    _write_yaml(
        workspace / "skills" / "table_registry.yaml",
        """
        version: 1
        tables:
          case_registry:
            app_token: app_workspace
            table_id: tbl_workspace
            field_aliases:
              title: 案件名称
          extra_table:
            app_token: app_extra
            table_id: tbl_extra
        """,
    )

    registry = TableRegistry(workspace=workspace, builtin_path=builtin)

    assert registry.resolve_table("case_registry") == {"app_token": "app_workspace", "table_id": "tbl_workspace"}
    assert registry.resolve_table("extra_table") == {"app_token": "app_extra", "table_id": "tbl_extra"}
    assert registry.map_field("case_registry", "title") == "案件名称"


def test_table_registry_maps_fields_and_filters(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin.yaml"
    _write_yaml(
        builtin,
        """
        version: 1
        tables:
          case_registry:
            app_token: app_x
            table_id: tbl_x
            field_aliases:
              title: 案号
              owner: 主办律师
        """,
    )

    registry = TableRegistry(workspace=tmp_path / "workspace", builtin_path=builtin)
    mapped = registry.map_fields("case_registry", {"title": "A", "owner": "B", "status": "open"})
    assert mapped == {"案号": "A", "主办律师": "B", "status": "open"}

    filters = {
        "conjunction": "and",
        "conditions": [
            {"field_name": "title", "operator": "contains", "value": "纳川"},
            {"field_name": "owner", "operator": "eq", "value": "刘达"},
        ],
    }
    mapped_filters = registry.map_filters("case_registry", filters)
    assert mapped_filters["conditions"][0]["field_name"] == "案号"
    assert mapped_filters["conditions"][1]["field_name"] == "主办律师"


def test_table_registry_strips_builtin_example_ids(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin.yaml"
    _write_yaml(
        builtin,
        """
        version: 1
        tables:
          case_tasks:
            app_token: app_case_ops_001
            table_id: tbl_case_tasks_001
            field_aliases:
              status: 任务状态
        """,
    )

    registry = TableRegistry(workspace=tmp_path / "workspace", builtin_path=builtin)
    resolved = registry.resolve_table("case_tasks")

    assert resolved == {}
    assert registry.map_field("case_tasks", "status") == "任务状态"


def test_table_registry_reload_when_workspace_file_changes(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin.yaml"
    _write_yaml(
        builtin,
        """
        version: 1
        tables:
          case_registry:
            app_token: app_x
            table_id: tbl_x
            field_aliases:
              title: 案号
        """,
    )

    workspace = tmp_path / "workspace"
    registry_path = workspace / "skills" / "table_registry.yaml"
    _write_yaml(
        registry_path,
        """
        version: 1
        tables:
          case_registry:
            app_token: app_x
            table_id: tbl_x
            field_aliases:
              title: 案件名称
        """,
    )

    registry = TableRegistry(workspace=workspace, builtin_path=builtin)
    assert registry.map_field("case_registry", "title") == "案件名称"

    _write_yaml(
        registry_path,
        """
        version: 1
        tables:
          case_registry:
            app_token: app_x
            table_id: tbl_x
            field_aliases:
              title: 案号字段
        """,
    )
    assert registry.map_field("case_registry", "title") == "案号字段"
