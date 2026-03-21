import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from nanobot.cli.commands import app
from nanobot.config.loader import save_config
from nanobot.config.schema import Config
from nanobot.providers.litellm_provider import LiteLLMProvider
from nanobot.providers.openai_codex_provider import _strip_model_prefix
from nanobot.providers.registry import find_by_model
from nanobot.utils.helpers import sync_workspace_templates

runner = CliRunner()


class _StopGatewayError(RuntimeError):
    pass


def test_feishu_broadcast_requires_message_source(monkeypatch) -> None:
    monkeypatch.setattr("nanobot.cli.commands._load_runtime_config", lambda *_args, **_kwargs: Config())

    result = runner.invoke(app, ["feishu", "broadcast"])

    assert result.exit_code == 1
    assert "Specify exactly one of --message or --message-file" in result.stdout


def test_version_flag_shows_formatted_version(monkeypatch) -> None:
    monkeypatch.setattr("nanobot.cli.commands.format_version", lambda: "nanobot v0.3.0 (f7ab86f)")

    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "nanobot v0.3.0 (f7ab86f)" in result.stdout


def test_feishu_broadcast_send_requires_confirm_token(monkeypatch, tmp_path: Path) -> None:
    config = Config()
    config.channels.feishu.enabled = True
    config.channels.feishu.app_id = "app_id"
    config.channels.feishu.app_secret = "app_secret"

    monkeypatch.setattr("nanobot.cli.commands._load_runtime_config", lambda *_args, **_kwargs: config)

    result = runner.invoke(app, ["feishu", "broadcast", "--message", "上线通知", "--send"])

    assert result.exit_code == 1
    assert "--confirm SEND" in result.stdout


def test_feishu_broadcast_dry_run_shows_recipient_count(monkeypatch) -> None:
    from nanobot.feishu.broadcast import BroadcastRecipient

    config = Config()
    config.channels.feishu.enabled = True
    config.channels.feishu.app_id = "app_id"
    config.channels.feishu.app_secret = "app_secret"

    class _Service:
        def list_active_recipients(self, page_size: int = 100, limit: int | None = None):
            assert page_size == 100
            assert limit is None
            return [
                BroadcastRecipient(open_id="ou_1", name="Alice"),
                BroadcastRecipient(open_id="ou_2", name="Bob"),
            ]

    monkeypatch.setattr("nanobot.cli.commands._load_runtime_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr("nanobot.cli.commands.FeishuClient.build", lambda _cfg: object())
    monkeypatch.setattr("nanobot.cli.commands.FeishuOutboundMessenger", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("nanobot.cli.commands.FeishuBroadcastService", lambda **_kwargs: _Service())

    result = runner.invoke(app, ["feishu", "broadcast", "--message", "上线通知"])

    assert result.exit_code == 0
    assert "Dry run" in result.stdout
    assert "2 active users" in result.stdout
    assert "Alice" in result.stdout


@pytest.fixture
def mock_paths():
    """Mock config/workspace paths for test isolation."""
    with patch("nanobot.config.loader.get_config_path") as mock_cp, \
         patch("nanobot.config.loader.save_config") as mock_sc, \
         patch("nanobot.config.loader.load_config"), \
         patch("nanobot.cli.commands.get_workspace_path") as mock_ws:

        base_dir = Path("./test_onboard_data")
        if base_dir.exists():
            shutil.rmtree(base_dir)
        base_dir.mkdir()

        config_file = base_dir / "config.json"
        workspace_dir = base_dir / "workspace"

        mock_cp.return_value = config_file
        mock_ws.return_value = workspace_dir
        mock_sc.side_effect = lambda config: config_file.write_text("{}")

        yield config_file, workspace_dir

        if base_dir.exists():
            shutil.rmtree(base_dir)


def test_onboard_fresh_install(mock_paths):
    """No existing config — should create from scratch."""
    config_file, workspace_dir = mock_paths

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0
    assert "Created config" in result.stdout
    assert "Created workspace" in result.stdout
    assert "nanobot is ready" in result.stdout
    assert config_file.exists()
    assert (workspace_dir / "AGENTS.md").exists()
    assert (workspace_dir / "memory" / "MEMORY.md").exists()


def test_onboard_existing_config_refresh(mock_paths):
    """Config exists, user declines overwrite — should refresh (load-merge-save)."""
    config_file, workspace_dir = mock_paths
    config_file.write_text('{"existing": true}')

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    assert "Config already exists" in result.stdout
    assert "existing values preserved" in result.stdout
    assert workspace_dir.exists()
    assert (workspace_dir / "AGENTS.md").exists()


def test_onboard_existing_config_overwrite(mock_paths):
    """Config exists, user confirms overwrite — should reset to defaults."""
    config_file, workspace_dir = mock_paths
    config_file.write_text('{"existing": true}')

    result = runner.invoke(app, ["onboard"], input="y\n")

    assert result.exit_code == 0
    assert "Config already exists" in result.stdout
    assert "Config reset to defaults" in result.stdout
    assert workspace_dir.exists()


def test_onboard_existing_workspace_safe_create(mock_paths):
    """Workspace exists — should not recreate, but still add missing templates."""
    config_file, workspace_dir = mock_paths
    workspace_dir.mkdir(parents=True)
    config_file.write_text("{}")

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    assert "Created workspace" not in result.stdout
    assert "Created AGENTS.md" in result.stdout
    assert (workspace_dir / "AGENTS.md").exists()


def test_config_matches_github_copilot_codex_with_hyphen_prefix():
    config = Config()
    config.agents.defaults.model = "github-copilot/gpt-5.3-codex"

    assert config.get_provider_name() == "github_copilot"


def test_config_matches_openai_codex_with_hyphen_prefix():
    config = Config()
    config.agents.defaults.model = "openai-codex/gpt-5.1-codex"

    assert config.get_provider_name() == "openai_codex"


def test_find_by_model_prefers_explicit_prefix_over_generic_codex_keyword():
    spec = find_by_model("github-copilot/gpt-5.3-codex")

    assert spec is not None
    assert spec.name == "github_copilot"


def test_litellm_provider_canonicalizes_github_copilot_hyphen_prefix():
    provider = LiteLLMProvider(default_model="github-copilot/gpt-5.3-codex")

    resolved = provider._resolve_model("github-copilot/gpt-5.3-codex")

    assert resolved == "github_copilot/gpt-5.3-codex"


def test_openai_codex_strip_prefix_supports_hyphen_and_underscore():
    assert _strip_model_prefix("openai-codex/gpt-5.1-codex") == "gpt-5.1-codex"
    assert _strip_model_prefix("openai_codex/gpt-5.1-codex") == "gpt-5.1-codex"


@pytest.fixture
def mock_agent_runtime(tmp_path):
    """Mock agent command dependencies for focused CLI tests."""
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "default-workspace")
    cron_dir = tmp_path / "data" / "cron"

    with patch("nanobot.config.loader.load_config", return_value=config) as mock_load_config, \
         patch("nanobot.config.paths.get_cron_dir", return_value=cron_dir), \
         patch("nanobot.cli.commands.sync_workspace_templates") as mock_sync_templates, \
         patch("nanobot.cli.commands._make_provider", return_value=object()), \
         patch("nanobot.cli.commands._print_agent_response") as mock_print_response, \
         patch("nanobot.bus.queue.MessageBus"), \
         patch("nanobot.cron.service.CronService"), \
         patch("nanobot.agent.loop.AgentLoop") as mock_agent_loop_cls:

        agent_loop = MagicMock()
        agent_loop.channels_config = None
        agent_loop.process_direct = AsyncMock(return_value="mock-response")
        agent_loop.close_mcp = AsyncMock(return_value=None)
        mock_agent_loop_cls.return_value = agent_loop

        yield {
            "config": config,
            "load_config": mock_load_config,
            "sync_templates": mock_sync_templates,
            "agent_loop_cls": mock_agent_loop_cls,
            "agent_loop": agent_loop,
            "print_response": mock_print_response,
        }


def test_agent_help_shows_workspace_and_config_options():
    result = runner.invoke(app, ["agent", "--help"])

    assert result.exit_code == 0
    assert "--workspace" in result.stdout
    assert "-w" in result.stdout
    assert "--config" in result.stdout
    assert "-c" in result.stdout


def test_agent_uses_default_config_when_no_workspace_or_config_flags(mock_agent_runtime):
    result = runner.invoke(app, ["agent", "-m", "hello"])

    assert result.exit_code == 0
    assert mock_agent_runtime["load_config"].call_args.args == (None,)
    assert mock_agent_runtime["sync_templates"].call_args.args == (
        mock_agent_runtime["config"].workspace_path,
    )
    assert mock_agent_runtime["agent_loop_cls"].call_args.kwargs["workspace"] == (
        mock_agent_runtime["config"].workspace_path
    )
    mock_agent_runtime["agent_loop"].process_direct.assert_awaited_once()
    mock_agent_runtime["print_response"].assert_called_once_with("mock-response", render_markdown=True)


def test_agent_uses_explicit_config_path(mock_agent_runtime, tmp_path: Path):
    config_path = tmp_path / "agent-config.json"
    config_path.write_text("{}")

    result = runner.invoke(app, ["agent", "-m", "hello", "-c", str(config_path)])

    assert result.exit_code == 0
    assert mock_agent_runtime["load_config"].call_args.args == (config_path.resolve(),)


def test_agent_config_sets_active_path(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    seen: dict[str, Path] = {}

    monkeypatch.setattr(
        "nanobot.config.loader.set_config_path",
        lambda path: seen.__setitem__("config_path", path),
    )
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr("nanobot.config.paths.get_cron_dir", lambda: config_file.parent / "cron")
    monkeypatch.setattr("nanobot.cli.commands.sync_workspace_templates", lambda _path: None)
    monkeypatch.setattr("nanobot.cli.commands._make_provider", lambda _config: object())
    monkeypatch.setattr("nanobot.bus.queue.MessageBus", lambda: object())
    monkeypatch.setattr("nanobot.cron.service.CronService", lambda _store: object())

    class _FakeAgentLoop:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def process_direct(self, *_args, **_kwargs) -> str:
            return "ok"

        async def close_mcp(self) -> None:
            return None

    monkeypatch.setattr("nanobot.agent.loop.AgentLoop", _FakeAgentLoop)
    monkeypatch.setattr("nanobot.cli.commands._print_agent_response", lambda *_args, **_kwargs: None)

    result = runner.invoke(app, ["agent", "-m", "hello", "-c", str(config_file)])

    assert result.exit_code == 0
    assert seen["config_path"] == config_file.resolve()


def test_agent_overrides_workspace_path(mock_agent_runtime):
    workspace_path = Path("/tmp/agent-workspace")

    result = runner.invoke(app, ["agent", "-m", "hello", "-w", str(workspace_path)])

    assert result.exit_code == 0
    assert mock_agent_runtime["config"].agents.defaults.workspace == str(workspace_path)
    assert mock_agent_runtime["sync_templates"].call_args.args == (workspace_path,)
    assert mock_agent_runtime["agent_loop_cls"].call_args.kwargs["workspace"] == workspace_path


def test_agent_workspace_override_wins_over_config_workspace(mock_agent_runtime, tmp_path: Path):
    config_path = tmp_path / "agent-config.json"
    config_path.write_text("{}")
    workspace_path = Path("/tmp/agent-workspace")

    result = runner.invoke(
        app,
        ["agent", "-m", "hello", "-c", str(config_path), "-w", str(workspace_path)],
    )

    assert result.exit_code == 0
    assert mock_agent_runtime["load_config"].call_args.args == (config_path.resolve(),)
    assert mock_agent_runtime["config"].agents.defaults.workspace == str(workspace_path)
    assert mock_agent_runtime["sync_templates"].call_args.args == (workspace_path,)
    assert mock_agent_runtime["agent_loop_cls"].call_args.kwargs["workspace"] == workspace_path


def test_upstream_status_errors_without_upstream_remote(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def _fake_git(args, cwd, check=True):
        assert cwd == repo
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(["git", *args], 0, stdout="true\n", stderr="")
        if args == ["remote"]:
            return subprocess.CompletedProcess(["git", *args], 0, stdout="origin\n", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr("nanobot.cli.commands._git_capture", _fake_git)

    result = runner.invoke(app, ["upstream", "status", "-w", str(repo)])

    assert result.exit_code == 1
    assert "Missing git remote 'upstream'" in result.stdout


def test_upstream_status_lists_upstream_only_commits_and_risk(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def _fake_git(args, cwd, check=True):
        assert cwd == repo
        table = {
            ("rev-parse", "--is-inside-work-tree"): subprocess.CompletedProcess(["git", *args], 0, stdout="true\n", stderr=""),
            ("remote",): subprocess.CompletedProcess(["git", *args], 0, stdout="origin\nupstream\n", stderr=""),
            ("fetch", "--no-tags", "upstream", "main"): subprocess.CompletedProcess(["git", *args], 0, stdout="", stderr=""),
            ("merge-base", "HEAD", "upstream/main"): subprocess.CompletedProcess(["git", *args], 0, stdout="base123\n", stderr=""),
            ("diff", "--name-only", "base123..HEAD"): subprocess.CompletedProcess(["git", *args], 0, stdout="nanobot/feishu/channel.py\nREADME.md\n", stderr=""),
            ("rev-list", "--reverse", "HEAD..upstream/main"): subprocess.CompletedProcess(["git", *args], 0, stdout="aaa111\nbbb222\n", stderr=""),
            ("show", "--quiet", "--date=short", "--format=%h%x09%ad%x09%s", "aaa111"): subprocess.CompletedProcess(["git", *args], 0, stdout="aaa111\t2026-03-20\tTouch Feishu flow\n", stderr=""),
            ("show", "--name-only", "--format=", "aaa111"): subprocess.CompletedProcess(["git", *args], 0, stdout="nanobot/feishu/outbound.py\nnanobot/feishu/streaming.py\n", stderr=""),
            ("show", "--quiet", "--date=short", "--format=%h%x09%ad%x09%s", "bbb222"): subprocess.CompletedProcess(["git", *args], 0, stdout="bbb222\t2026-03-21\tDocs cleanup\n", stderr=""),
            ("show", "--name-only", "--format=", "bbb222"): subprocess.CompletedProcess(["git", *args], 0, stdout="docs/guide.md\n", stderr=""),
        }
        key = tuple(args)
        if key not in table:
            raise AssertionError(args)
        return table[key]

    monkeypatch.setattr("nanobot.cli.commands._git_capture", _fake_git)

    result = runner.invoke(app, ["upstream", "status", "-w", str(repo)])

    assert result.exit_code == 0
    assert "Upstream-only commits: 2" in result.stdout
    assert "[HIGH] aaa111" in result.stdout
    assert "Paths: nanobot" in result.stdout
    assert "[MEDIUM] bbb222" in result.stdout
    assert "Paths: docs" in result.stdout
    assert "git show aaa111" in result.stdout
    assert "git cherry-pick -x bbb222" in result.stdout


def test_gateway_uses_workspace_from_config_by_default(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "config-workspace")
    seen: dict[str, Path] = {}

    monkeypatch.setattr(
        "nanobot.config.loader.set_config_path",
        lambda path: seen.__setitem__("config_path", path),
    )
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr(
        "nanobot.cli.commands.sync_workspace_templates",
        lambda path: seen.__setitem__("workspace", path),
    )
    monkeypatch.setattr(
        "nanobot.cli.commands._make_provider",
        lambda _config: (_ for _ in ()).throw(_StopGatewayError("stop")),
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGatewayError)
    assert seen["config_path"] == config_file.resolve()
    assert seen["workspace"] == Path(config.agents.defaults.workspace)


def test_gateway_workspace_option_overrides_config(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "config-workspace")
    override = tmp_path / "override-workspace"
    seen: dict[str, Path] = {}

    monkeypatch.setattr("nanobot.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr(
        "nanobot.cli.commands.sync_workspace_templates",
        lambda path: seen.__setitem__("workspace", path),
    )
    monkeypatch.setattr(
        "nanobot.cli.commands._make_provider",
        lambda _config: (_ for _ in ()).throw(_StopGatewayError("stop")),
    )

    result = runner.invoke(
        app,
        ["gateway", "--config", str(config_file), "--workspace", str(override)],
    )

    assert isinstance(result.exception, _StopGatewayError)
    assert seen["workspace"] == override
    assert config.workspace_path == override

def test_gateway_uses_config_directory_for_cron_store(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "config-workspace")
    seen: dict[str, Path] = {}

    monkeypatch.setattr("nanobot.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr("nanobot.config.paths.get_cron_dir", lambda: config_file.parent / "cron")
    monkeypatch.setattr("nanobot.cli.commands.sync_workspace_templates", lambda _path: None)
    monkeypatch.setattr("nanobot.cli.commands._make_provider", lambda _config: object())
    monkeypatch.setattr("nanobot.bus.queue.MessageBus", lambda: object())
    monkeypatch.setattr("nanobot.session.manager.SessionManager", lambda _workspace: object())

    class _StopCron:
        def __init__(self, store_path: Path) -> None:
            seen["cron_store"] = store_path
            raise _StopGatewayError("stop")

    monkeypatch.setattr("nanobot.cron.service.CronService", _StopCron)

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGatewayError)
    assert seen["cron_store"] == config_file.parent / "cron" / "jobs.json"


def test_gateway_uses_configured_port_when_cli_flag_is_missing(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.gateway.port = 18791

    monkeypatch.setattr("nanobot.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr("nanobot.cli.commands.sync_workspace_templates", lambda _path: None)
    monkeypatch.setattr(
        "nanobot.cli.commands._make_provider",
        lambda _config: (_ for _ in ()).throw(_StopGatewayError("stop")),
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGatewayError)
    assert "port 18791" in result.stdout


def test_gateway_cli_port_overrides_configured_port(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.gateway.port = 18791

    monkeypatch.setattr("nanobot.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr("nanobot.cli.commands.sync_workspace_templates", lambda _path: None)
    monkeypatch.setattr(
        "nanobot.cli.commands._make_provider",
        lambda _config: (_ for _ in ()).throw(_StopGatewayError("stop")),
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file), "--port", "18792"])

    assert isinstance(result.exception, _StopGatewayError)
    assert "port 18792" in result.stdout


def _write_cli_config(config_path: Path, *, workspace: Path | None = None) -> Config:
    config = Config()
    config.providers.anthropic.api_key = "test-key"
    if workspace is not None:
        config.agents.defaults.workspace = str(workspace)
    save_config(config, config_path)
    return config


def test_heartbeat_status_reads_target_config(tmp_path: Path) -> None:
    config_path = tmp_path / "nanobot.json"
    workspace = tmp_path / "workspace"
    config = _write_cli_config(config_path, workspace=workspace)
    config.gateway.heartbeat.enabled = False
    config.gateway.heartbeat.interval_s = 7200
    save_config(config, config_path)

    result = runner.invoke(app, ["heartbeat", "status", "--config", str(config_path)])

    assert result.exit_code == 0
    assert str(config_path.resolve()) in result.stdout.replace("\n", "")
    assert "Enabled: no" in result.stdout
    assert "Interval: 7200s" in result.stdout


def test_heartbeat_off_preserves_unrelated_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "nanobot.json"
    workspace = tmp_path / "workspace"
    config = _write_cli_config(config_path, workspace=workspace)
    config.agents.defaults.model = "anthropic/claude-3-7-sonnet"
    config.gateway.heartbeat.enabled = True
    save_config(config, config_path)

    result = runner.invoke(app, ["heartbeat", "off", "--config", str(config_path)])

    assert result.exit_code == 0
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["agents"]["defaults"]["model"] == "anthropic/claude-3-7-sonnet"
    assert saved["gateway"]["heartbeat"]["enabled"] is False
    assert "Restart required" in result.stdout


def test_heartbeat_on_updates_target_config(tmp_path: Path) -> None:
    config_path = tmp_path / "nanobot.json"
    workspace = tmp_path / "workspace"
    config = _write_cli_config(config_path, workspace=workspace)
    config.gateway.heartbeat.enabled = False
    save_config(config, config_path)

    result = runner.invoke(app, ["heartbeat", "on", "--config", str(config_path)])

    assert result.exit_code == 0
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["gateway"]["heartbeat"]["enabled"] is True
    assert "Enabled heartbeat" in result.stdout


def test_heartbeat_set_interval_rejects_invalid_values(tmp_path: Path) -> None:
    config_path = tmp_path / "nanobot.json"
    workspace = tmp_path / "workspace"
    _write_cli_config(config_path, workspace=workspace)

    result = runner.invoke(app, ["heartbeat", "set-interval", "0", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "positive integer" in result.stdout


def test_doctor_reports_clean_state_on_valid_setup(tmp_path: Path) -> None:
    config_path = tmp_path / "nanobot.json"
    workspace = tmp_path / "workspace"
    _write_cli_config(config_path, workspace=workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    sync_workspace_templates(workspace, silent=True)

    result = runner.invoke(app, ["doctor", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Doctor Findings" in result.stdout
    assert "workspace.templates" in result.stdout
    assert "heartbeat.legacy_sessions" in result.stdout


def test_doctor_fix_repairs_fixable_issues(tmp_path: Path) -> None:
    config_path = tmp_path / "nanobot.json"
    workspace = tmp_path / "workspace"
    raw = Config().model_dump(by_alias=True)
    raw["agents"]["defaults"]["workspace"] = str(workspace)
    raw["providers"]["anthropic"]["apiKey"] = "test-key"
    raw["gateway"]["heartbeat"]["enabled"] = "yes"
    raw["gateway"]["heartbeat"]["intervalS"] = 0
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    (workspace / "sessions").mkdir(parents=True, exist_ok=True)
    (workspace / "sessions" / "legacy-heartbeat.jsonl").write_text("{}", encoding="utf-8")

    result = runner.invoke(app, ["doctor", "--fix", "--config", str(config_path)])

    assert result.exit_code == 0
    repaired = json.loads(config_path.read_text(encoding="utf-8"))
    assert repaired["gateway"]["heartbeat"]["enabled"] is True
    assert repaired["gateway"]["heartbeat"]["intervalS"] == 1800
    assert (workspace / "AGENTS.md").exists()
    assert not (workspace / "sessions" / "legacy-heartbeat.jsonl").exists()
    assert "FIXED" in result.stdout
    assert "Restart required" in result.stdout


def test_doctor_returns_nonzero_when_unfixable_issues_remain(tmp_path: Path) -> None:
    config_path = tmp_path / "broken.json"
    config_path.write_text("{not-json", encoding="utf-8")

    result = runner.invoke(app, ["doctor", "--fix", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "not valid JSON" in result.stdout


def test_reset_recreates_workspace_and_preserves_config_by_default(tmp_path: Path) -> None:
    config_path = tmp_path / "nanobot.json"
    workspace = tmp_path / "workspace"
    _write_cli_config(config_path, workspace=workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    sync_workspace_templates(workspace, silent=True)
    (workspace / "sessions").mkdir(parents=True, exist_ok=True)
    (workspace / "sessions" / "cli_direct.jsonl").write_text("{}", encoding="utf-8")
    (workspace / "notes.txt").write_text("temporary", encoding="utf-8")
    before_config = json.loads(config_path.read_text(encoding="utf-8"))

    result = runner.invoke(app, ["reset", "--config-path", str(config_path), "--yes"])

    assert result.exit_code == 0
    assert "Reset workspace" in result.stdout
    assert (workspace / "AGENTS.md").exists()
    assert (workspace / "memory" / "MEMORY.md").exists()
    assert not (workspace / "notes.txt").exists()
    assert not (workspace / "sessions" / "cli_direct.jsonl").exists()
    assert json.loads(config_path.read_text(encoding="utf-8")) == before_config


def test_reset_can_reset_config_and_history(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "nanobot.json"
    workspace = tmp_path / "workspace"
    config = _write_cli_config(config_path, workspace=workspace)
    config.agents.defaults.model = "anthropic/claude-3-7-sonnet"
    save_config(config, config_path)
    workspace.mkdir(parents=True, exist_ok=True)
    sync_workspace_templates(workspace, silent=True)
    history_path = tmp_path / "history" / "cli_history"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text("old prompt\n", encoding="utf-8")
    monkeypatch.setattr("nanobot.cli.commands.get_cli_history_path", lambda: history_path)

    result = runner.invoke(
        app,
        ["reset", "--config-path", str(config_path), "--config", "--history", "--yes"],
    )

    assert result.exit_code == 0
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["agents"]["defaults"]["model"] == Config().agents.defaults.model
    assert not history_path.exists()
    assert "Reset config" in result.stdout
    assert "Restart required" in result.stdout


def test_reset_dry_run_and_cancel_leave_files_unchanged(tmp_path: Path) -> None:
    config_path = tmp_path / "nanobot.json"
    workspace = tmp_path / "workspace"
    _write_cli_config(config_path, workspace=workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    sync_workspace_templates(workspace, silent=True)
    marker = workspace / "notes.txt"
    marker.write_text("keep me", encoding="utf-8")

    dry_run = runner.invoke(app, ["reset", "--config-path", str(config_path), "--dry-run"])
    cancelled = runner.invoke(app, ["reset", "--config-path", str(config_path)], input="n\n")

    assert dry_run.exit_code == 0
    assert "Dry run only" in dry_run.stdout
    assert cancelled.exit_code == 0
    assert "Reset cancelled" in cancelled.stdout
    assert marker.read_text(encoding="utf-8") == "keep me"
