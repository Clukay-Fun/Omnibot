from pathlib import Path

import yaml

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
        assert payload["response"]["output_policy"]["max_items"] == 5
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
