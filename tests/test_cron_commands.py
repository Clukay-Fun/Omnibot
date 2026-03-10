"""描述:
主要功能:
    - 校验命令行定时任务子命令的参数校验行为。
"""

from pathlib import Path

from typer.testing import CliRunner

from nanobot.cli import commands
from nanobot.cli.commands import app

runner = CliRunner()


#region 定时命令测试


def test_cron_add_rejects_invalid_timezone(monkeypatch, tmp_path) -> None:
    """用处，参数

    功能:
        - 验证错误时区输入会返回失败并阻止落盘。
    """
    monkeypatch.setattr("nanobot.config.loader.get_data_dir", lambda: tmp_path)

    result = runner.invoke(
        app,
        [
            "cron",
            "add",
            "--name",
            "demo",
            "--message",
            "hello",
            "--cron",
            "0 9 * * *",
            "--tz",
            "America/Vancovuer",
        ],
    )

    assert result.exit_code == 1
    assert "Error: unknown timezone 'America/Vancovuer'" in result.stdout
    assert not (tmp_path / "cron" / "jobs.json").exists()


def test_cron_list_uses_legacy_store_compat_paths(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class _FakeCronService:
        def __init__(self, store_path: Path, legacy_store_paths=None):
            captured["store_path"] = store_path
            captured["legacy_store_paths"] = list(legacy_store_paths or [])

        def list_jobs(self, include_disabled: bool = False):
            _ = include_disabled
            return []

    state_root = tmp_path / "state"
    data_root = tmp_path / "data"

    monkeypatch.setattr(commands, "get_state_path", lambda: state_root)
    monkeypatch.setattr(commands, "get_data_path", lambda: data_root, raising=False)
    monkeypatch.setattr("nanobot.cron.service.CronService", _FakeCronService)

    result = runner.invoke(app, ["cron", "list"])

    assert result.exit_code == 0
    assert captured["store_path"] == state_root / "cron" / "jobs.json"
    assert captured["legacy_store_paths"] == [data_root / "cron" / "jobs.json"]


#endregion
