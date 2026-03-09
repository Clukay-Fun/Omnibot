from pathlib import Path

import yaml

from nanobot.agent.skill_runtime.registry import SkillSpecRegistry

SKILLSPEC_DIR = Path(__file__).resolve().parents[1] / "nanobot" / "skills" / "skillspec"
REQUIRED_TOP_LEVEL_KEYS = {"meta", "params", "action", "response", "error"}


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    assert isinstance(loaded, dict), f"{path.name} must contain a YAML mapping"
    return loaded


def test_skillspec_files_exist() -> None:
    expected = {
        "_index.yaml",
        "case_search.yaml",
        "case_detail.yaml",
        "deadline_overview.yaml",
        "contract_search.yaml",
        "task_search.yaml",
        "task_update.yaml",
        "doc_recognize.yaml",
        "reminder_set.yaml",
        "reminder_list.yaml",
        "reminder_cancel.yaml",
        "daily_summary.yaml",
        "work_report_create.yaml",
    }
    found = {p.name for p in SKILLSPEC_DIR.glob("*.yaml")}
    assert expected.issubset(found)


def test_query_skill_files_follow_v01_shape() -> None:
    has_cross_query = False
    for path in sorted(SKILLSPEC_DIR.glob("*.yaml")):
        if path.name == "_index.yaml":
            continue
        payload = _load_yaml(path)
        assert REQUIRED_TOP_LEVEL_KEYS.issubset(payload)

        action = payload.get("action")
        assert isinstance(action, dict)
        if str(action.get("kind", "")).lower() != "query":
            continue

        assert payload["action"]["pagination_mode"] == "data"
        assert payload["response"]["output_policy"]["max_items"] >= 5
        if "cross_query" in payload["action"]:
            has_cross_query = True

    assert has_cross_query, "at least one skillspec should demonstrate cross_query"


def test_skillspec_index_contains_id_and_description() -> None:
    payload = _load_yaml(SKILLSPEC_DIR / "_index.yaml")
    skills = payload.get("skills")
    assert isinstance(skills, list)
    assert skills
    for item in skills:
        assert isinstance(item, dict)
        assert item.get("id")
        assert item.get("description")


def test_builtin_skillspec_assets_expose_blueprint_inventory(tmp_path: Path) -> None:
    registry = SkillSpecRegistry(workspace_root=tmp_path / "workspace", builtin_root=SKILLSPEC_DIR)
    registry.load()

    case_detail = registry.blueprints["case_detail"]
    assert case_detail.action_kind == "query"
    assert case_detail.primary_tool == "bitable_search"
    assert case_detail.action_metadata["has_cross_query"] is True
    assert case_detail.action_metadata["cross_query_mode"] == "fanout"
    assert [step.id for step in case_detail.steps] == ["case_base", "related_tasks", "related_contracts"]
    assert [table.alias for table in case_detail.tables] == [
        "case_registry",
        "case_tasks",
        "contract_registry",
    ]

    reminder_set = registry.blueprints["reminder_set"]
    assert reminder_set.action_kind == "reminder_set"
    assert reminder_set.tool_refs == ["bitable_create", "calendar_create", "cron"]
    assert reminder_set.action_metadata["bridge_keys"] == [
        "calendar_bridge",
        "record_bridge",
        "summary_cron_bridge",
    ]

    doc_recognize = registry.blueprints["doc_recognize"]
    assert doc_recognize.action_kind == "document_pipeline"
    assert doc_recognize.action_target == "process_document"
    assert doc_recognize.primary_tool == "bitable_create"
    assert doc_recognize.action_metadata["has_write_bridge"] is True
