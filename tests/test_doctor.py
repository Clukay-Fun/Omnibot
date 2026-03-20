import json
from pathlib import Path

from nanobot.cli.doctor import resolve_config_path, run_doctor
from nanobot.config.loader import save_config
from nanobot.config.schema import Config
from nanobot.utils.helpers import sync_workspace_templates


def _write_config(path: Path, *, workspace: Path) -> Config:
    config = Config()
    config.agents.defaults.workspace = str(workspace)
    config.providers.anthropic.api_key = "test-key"
    save_config(config, path)
    return config


def test_resolve_config_path_expands_and_resolves(tmp_path: Path) -> None:
    config_path = tmp_path / "cfg" / "nanobot.json"

    resolved = resolve_config_path(config_path)

    assert resolved == config_path.resolve()


def test_run_doctor_reports_clean_workspace(tmp_path: Path) -> None:
    config_path = tmp_path / "nanobot.json"
    workspace = tmp_path / "workspace"
    _write_config(config_path, workspace=workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    sync_workspace_templates(workspace, silent=True)

    report = run_doctor(config_path)

    assert report.workspace_path == workspace
    assert report.has_remaining_issues is False
    assert {finding.key: finding.status for finding in report.findings}["workspace.templates"] == "ok"
    assert (workspace / "WORKLOG.md").exists()


def test_run_doctor_fix_repairs_heartbeat_and_cleans_legacy_files(tmp_path: Path) -> None:
    config_path = tmp_path / "nanobot.json"
    workspace = tmp_path / "workspace"
    raw = Config().model_dump(by_alias=True)
    raw["agents"]["defaults"]["workspace"] = str(workspace)
    raw["providers"]["anthropic"]["apiKey"] = "test-key"
    raw["gateway"]["heartbeat"]["enabled"] = "true"
    raw["gateway"]["heartbeat"]["intervalS"] = -1
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    (workspace / "sessions").mkdir(parents=True, exist_ok=True)
    legacy = workspace / "sessions" / "feishu_dm_user_heartbeat.jsonl"
    legacy.write_text("{}", encoding="utf-8")

    report = run_doctor(config_path, fix=True)

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["gateway"]["heartbeat"]["enabled"] is True
    assert saved["gateway"]["heartbeat"]["intervalS"] == 1800
    assert legacy.exists() is False
    assert report.restart_required is True
    assert {finding.key: finding.status for finding in report.findings}["heartbeat.interval"] == "fixed"
    assert {finding.key: finding.status for finding in report.findings}["heartbeat.legacy_sessions"] == "fixed"


def test_run_doctor_without_fix_does_not_mutate_files(tmp_path: Path) -> None:
    config_path = tmp_path / "nanobot.json"
    workspace = tmp_path / "workspace"
    raw = Config().model_dump(by_alias=True)
    raw["agents"]["defaults"]["workspace"] = str(workspace)
    raw["providers"]["anthropic"]["apiKey"] = "test-key"
    raw["gateway"]["heartbeat"]["intervalS"] = 0
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    original = config_path.read_text(encoding="utf-8")

    report = run_doctor(config_path, fix=False)

    assert config_path.read_text(encoding="utf-8") == original
    assert report.has_remaining_issues is True
    assert {finding.key: finding.status for finding in report.findings}["heartbeat.interval"] == "warn"


def test_run_doctor_stops_on_invalid_json(tmp_path: Path) -> None:
    config_path = tmp_path / "nanobot.json"
    config_path.write_text("{bad-json", encoding="utf-8")

    report = run_doctor(config_path, fix=True)

    assert report.has_remaining_issues is True
    assert report.workspace_path is None
    assert report.findings[0].key == "config.file"
    assert report.findings[0].status == "error"
