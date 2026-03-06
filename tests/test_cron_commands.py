"""描述:
主要功能:
    - 校验命令行定时任务子命令的参数校验行为。
"""

from typer.testing import CliRunner

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


#endregion
