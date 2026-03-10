import shutil
import sys
import types
import json
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from typer.testing import CliRunner

try:
    import prompt_toolkit  # type: ignore # noqa: F401
except ModuleNotFoundError:
    prompt_toolkit_stub = types.ModuleType("prompt_toolkit")

    class _DummyPromptSession:
        def __init__(self, *args, **kwargs):
            _ = args, kwargs

        async def prompt_async(self, *args, **kwargs):
            _ = args, kwargs
            return ""

    prompt_toolkit_stub.PromptSession = _DummyPromptSession

    formatted_stub = types.ModuleType("prompt_toolkit.formatted_text")
    formatted_stub.HTML = lambda value: value

    history_stub = types.ModuleType("prompt_toolkit.history")

    class _DummyFileHistory:
        def __init__(self, *args, **kwargs):
            _ = args, kwargs

    history_stub.FileHistory = _DummyFileHistory

    patch_stdout_stub = types.ModuleType("prompt_toolkit.patch_stdout")

    @contextmanager
    def _dummy_patch_stdout():
        yield

    patch_stdout_stub.patch_stdout = _dummy_patch_stdout

    sys.modules["prompt_toolkit"] = prompt_toolkit_stub
    sys.modules["prompt_toolkit.formatted_text"] = formatted_stub
    sys.modules["prompt_toolkit.history"] = history_stub
    sys.modules["prompt_toolkit.patch_stdout"] = patch_stdout_stub

from nanobot.agent.runtime_texts import RuntimeTextCatalog
from nanobot.cli.commands import _build_feishu_oauth_stack, app
from nanobot.config.schema import Config
from nanobot.oauth import (
    FeishuOAuthClient,
    FeishuOAuthService,
    FeishuReauthorizationRequired,
    FeishuUserTokenManager,
    OAuthCallbackService,
)
from nanobot.providers.litellm_provider import LiteLLMProvider
from nanobot.providers.openai_codex_provider import _strip_model_prefix
from nanobot.providers.registry import find_by_model
from nanobot.storage import SQLiteStore
from nanobot.utils.helpers import sync_workspace_templates

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def mock_paths():
    """Mock config/workspace paths for test isolation."""
    with patch("nanobot.config.loader.get_config_path") as mock_cp, \
         patch("nanobot.config.loader.save_config") as mock_sc, \
         patch("nanobot.config.loader.load_config") as _mock_lc, \
         patch("nanobot.utils.helpers.get_workspace_path") as mock_ws:

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
    assert "~/.nanobot/config.json" in result.stdout
    assert config_file.exists()
    assert (workspace_dir / "AGENTS.md").exists()
    assert (workspace_dir / "memory" / "MEMORY.md").exists()
    assert not (workspace_dir / "prompts").exists()
    assert not (workspace_dir / "routing").exists()
    assert not (workspace_dir / "templates").exists()


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


def test_repo_uses_config_example_instead_of_tracked_runtime_config():
    sample_path = REPO_ROOT / "config.example.json"
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    operations = (REPO_ROOT / "docs" / "guides" / "OPERATIONS_FEISHU.md").read_text(encoding="utf-8")

    assert sample_path.exists()

    payload = json.loads(sample_path.read_text(encoding="utf-8"))
    assert payload["agents"]["defaults"]["workspace"] == "~/.nanobot/workspace"
    assert payload["integrations"]["feishu"]["auth"]["appId"] == ""
    assert payload["integrations"]["feishu"]["auth"]["appSecret"] == ""
    assert payload["integrations"]["feishu"]["auth"]["encryptKey"] == ""
    assert payload["integrations"]["feishu"]["auth"]["verificationToken"] == ""
    assert payload["integrations"]["feishu"]["api"]["apiBase"] == "https://open.feishu.cn/open-apis"
    assert "/config.json" in gitignore
    assert "config.example.json" in readme
    assert "~/.nanobot/config.json" in readme
    assert "config.example.json" in operations
    assert "~/.nanobot/config.json" in operations

    if shutil.which("git") and (REPO_ROOT / ".git").exists():
        tracked_root_config = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "config.json"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        staged_root_config_removal = subprocess.run(
            ["git", "diff", "--cached", "--name-status", "--", "config.json"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        worktree_root_config_removal = subprocess.run(
            ["git", "diff", "--name-only", "--", "config.json"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert (
            tracked_root_config.returncode != 0
            or "D\tconfig.json" in staged_root_config_removal.stdout
            or "config.json" in worktree_root_config_removal.stdout
        )


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


def test_config_supports_skillspec_render_timeout_defaults_and_overrides() -> None:
    config = Config()
    assert config.agents.defaults.skillspec_render_primary_timeout_seconds == 12.0
    assert config.agents.defaults.skillspec_render_retry_timeout_seconds == 6.0

    overridden = Config.model_validate({
        "agents": {
            "defaults": {
                "skillspecRenderPrimaryTimeoutSeconds": 8,
                "skillspecRenderRetryTimeoutSeconds": 3,
            }
        }
    })
    assert overridden.agents.defaults.skillspec_render_primary_timeout_seconds == 8
    assert overridden.agents.defaults.skillspec_render_retry_timeout_seconds == 3


def test_runtime_text_catalog_ignores_workspace_prompt_overrides(tmp_path: Path) -> None:
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / "help.yaml").write_text('commands_help_text: "workspace override"\n', encoding="utf-8")
    routing_dir = tmp_path / "routing"
    routing_dir.mkdir(parents=True, exist_ok=True)
    (routing_dir / "pagination_triggers.yaml").write_text('continuation_commands: ["next"]\n', encoding="utf-8")
    template_dir = tmp_path / "templates"
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "card_case.json").write_text('{"header":"workspace override"}\n', encoding="utf-8")

    catalog = RuntimeTextCatalog.load(tmp_path)

    assert catalog.prompt_text("help", "commands_help_text", "") != "workspace override"
    assert catalog.routing_list("pagination_triggers", "continuation_commands", []) == ["继续", "展开"]
    assert catalog.template("card_case").get("header") != "workspace override"


def test_sync_workspace_templates_removes_legacy_runtime_dirs(tmp_path: Path) -> None:
    for legacy in ("prompts", "routing", "templates"):
        path = tmp_path / legacy
        path.mkdir(parents=True, exist_ok=True)
        (path / "dummy.txt").write_text("x", encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    for legacy in ("prompts", "routing", "templates"):
        assert not (tmp_path / legacy).exists()


def test_config_supports_feishu_oauth_server_settings() -> None:
    config = Config.model_validate(
        {
            "integrations": {
                "feishu": {
                    "oauth": {
                        "enabled": True,
                        "publicBaseUrl": "https://bot.example.com",
                        "callbackPath": "/oauth/feishu/callback",
                        "stateTtlSeconds": 900,
                        "refreshAheadSeconds": 180,
                    }
                }
            }
        }
    )

    oauth = config.integrations.feishu.oauth
    assert oauth.enabled is True
    assert oauth.public_base_url == "https://bot.example.com"
    assert oauth.callback_path == "/oauth/feishu/callback"
    assert oauth.state_ttl_seconds == 900
    assert oauth.refresh_ahead_seconds == 180


def test_config_resolves_feishu_storage_path_and_sqlite_options(tmp_path: Path) -> None:
    config = Config.model_validate(
        {
            "agents": {"defaults": {"workspace": str(tmp_path)}},
            "integrations": {
                "feishu": {
                    "storage": {
                        "stateDbPath": "runtime/feishu-state.sqlite3",
                        "sqliteJournalMode": "WAL",
                        "sqliteSynchronous": "FULL",
                        "sqliteBusyTimeoutMs": 9000,
                    }
                }
            },
        }
    )

    state_path = config.resolve_feishu_state_db_path()
    options = config.resolve_feishu_sqlite_options()

    assert state_path == (tmp_path / "runtime" / "feishu-state.sqlite3").resolve()
    assert options.journal_mode == "WAL"
    assert options.synchronous == "FULL"
    assert options.busy_timeout_ms == 9000


def test_config_resolves_default_feishu_state_db_outside_workspace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = Config.model_validate(
        {
            "agents": {"defaults": {"workspace": str(tmp_path / "workspace")}},
        }
    )

    state_path = config.resolve_feishu_state_db_path()

    assert state_path == tmp_path / ".nanobot" / "state" / "feishu" / "state.sqlite3"


def test_build_oauth_stack_rejects_non_https_public_base_url(tmp_path: Path) -> None:
    config = Config.model_validate(
        {
            "agents": {"defaults": {"workspace": str(tmp_path)}},
            "integrations": {
                "feishu": {
                    "auth": {"appId": "cli_app", "appSecret": "sec"},
                    "oauth": {
                        "enabled": True,
                        "publicBaseUrl": "http://bot.example.com",
                        "enforceHttpsPublicBaseUrl": True,
                    },
                }
            },
        }
    )

    assert _build_feishu_oauth_stack(config) is None


def test_build_oauth_stack_rejects_host_not_in_allowlist(tmp_path: Path) -> None:
    config = Config.model_validate(
        {
            "agents": {"defaults": {"workspace": str(tmp_path)}},
            "integrations": {
                "feishu": {
                    "auth": {"appId": "cli_app", "appSecret": "sec"},
                    "oauth": {
                        "enabled": True,
                        "publicBaseUrl": "https://bot.example.com",
                        "allowedRedirectDomains": ["corp.example.com"],
                    },
                }
            },
        }
    )

    assert _build_feishu_oauth_stack(config) is None


def test_build_oauth_stack_uses_configured_state_db_path(tmp_path: Path) -> None:
    db_path = tmp_path / "db" / "oauth-state.sqlite3"
    config = Config.model_validate(
        {
            "agents": {"defaults": {"workspace": str(tmp_path)}},
            "integrations": {
                "feishu": {
                    "auth": {"appId": "cli_app", "appSecret": "sec"},
                    "storage": {
                        "stateDbPath": str(db_path),
                        "sqliteBusyTimeoutMs": 4321,
                    },
                    "oauth": {
                        "enabled": True,
                        "publicBaseUrl": "https://bot.example.com",
                        "allowedRedirectDomains": ["bot.example.com"],
                    },
                }
            },
        }
    )

    stack = _build_feishu_oauth_stack(config)
    assert stack is not None
    assert stack.store.db_path == db_path
    timeout = int(stack.store._conn.execute("PRAGMA busy_timeout").fetchone()[0])
    assert timeout == 4321
    stack.store.close()


class _FakeSyncHTTPFactory:
    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, **kwargs):
        _ = kwargs
        factory = self

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                _ = exc_type, exc, tb
                return False

            def post(self, url: str, json: dict | None = None, headers: dict | None = None):
                factory.calls.append(("POST", url, json))
                _ = headers
                return factory._responses.pop(0)

            def get(self, url: str, headers: dict | None = None):
                factory.calls.append(("GET", url, None))
                _ = headers
                return factory._responses.pop(0)

        return _Client()


def _extract_state_from_url(url: str) -> str:
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    return str((query.get("state") or [""])[0])


def test_feishu_oauth_callback_success_persists_token_and_consumes_state(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "memory" / "feishu" / "state.sqlite3")
    fake_http = _FakeSyncHTTPFactory(
        [
            httpx.Response(
                200,
                json={
                    "access_token": "u_access_1",
                    "refresh_token": "u_refresh_1",
                    "token_type": "Bearer",
                    "scope": "task:read",
                    "expires_in": 7200,
                    "refresh_expires_in": 2592000,
                },
            ),
            httpx.Response(200, json={"open_id": "ou_test_1"}),
        ]
    )
    client = FeishuOAuthClient(
        api_base="https://open.feishu.cn",
        app_id="cli_test",
        app_secret="sec_test",
        http_client_factory=fake_http,
    )
    service = FeishuOAuthService(
        store=store,
        client=client,
        redirect_uri="https://bot.example.com/oauth/feishu/callback",
        scopes=["task:read"],
        state_ttl_seconds=600,
    )

    auth_url = service.create_authorization_url(actor_open_id="ou_sender", chat_id="oc_group")
    state = _extract_state_from_url(auth_url)

    callback = service.handle_callback({"state": state, "code": "code_ok"})
    assert callback.success is True
    assert callback.open_id == "ou_test_1"

    token_row = store.get_feishu_user_token("ou_test_1")
    assert token_row is not None
    assert token_row["access_token"] == "u_access_1"
    assert token_row["refresh_token"] == "u_refresh_1"
    assert token_row["status"] == "active"

    state_row = store.get_oauth_state(state)
    assert state_row is not None
    assert state_row["status"] == "consumed"

    replay = service.handle_callback({"state": state, "code": "code_replay"})
    assert replay.success is False
    assert replay.status_code == 400


def test_feishu_user_token_manager_refreshes_and_persists(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "memory" / "feishu" / "state.sqlite3")
    now = datetime.now()
    store.upsert_feishu_user_token(
        "ou_refresh",
        app_id="cli_test",
        access_token="old_access",
        refresh_token="old_refresh",
        token_type="Bearer",
        scope="task:read",
        expires_at=(now - timedelta(seconds=5)).isoformat(),
        refresh_expires_at=(now + timedelta(days=30)).isoformat(),
        status="active",
        last_refreshed_at=now.isoformat(),
        last_error=None,
        payload={"seed": True},
    )

    fake_http = _FakeSyncHTTPFactory(
        [
            httpx.Response(
                200,
                json={
                    "access_token": "new_access",
                    "refresh_token": "new_refresh",
                    "token_type": "Bearer",
                    "scope": "task:read task:write",
                    "expires_in": 3600,
                    "refresh_expires_in": 2592000,
                },
            )
        ]
    )
    client = FeishuOAuthClient(
        api_base="https://open.feishu.cn",
        app_id="cli_test",
        app_secret="sec_test",
        http_client_factory=fake_http,
    )
    manager = FeishuUserTokenManager(store=store, client=client, refresh_ahead_seconds=300)

    token = manager.get_valid_access_token("ou_refresh")
    assert token == "new_access"

    row = store.get_feishu_user_token("ou_refresh")
    assert row is not None
    assert row["access_token"] == "new_access"
    assert row["refresh_token"] == "new_refresh"
    assert row["status"] == "active"
    assert row["last_error"] in (None, "")


def test_feishu_user_token_manager_marks_reauth_when_refresh_fails_and_expired(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "memory" / "feishu" / "state.sqlite3")
    now = datetime.now()
    store.upsert_feishu_user_token(
        "ou_expired",
        app_id="cli_test",
        access_token="expired_access",
        refresh_token="expired_refresh",
        token_type="Bearer",
        scope="task:read",
        expires_at=(now - timedelta(seconds=10)).isoformat(),
        refresh_expires_at=(now + timedelta(days=1)).isoformat(),
        status="active",
        last_refreshed_at=now.isoformat(),
        last_error=None,
        payload={"seed": True},
    )

    fake_http = _FakeSyncHTTPFactory(
        [
            httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": "refresh token invalid",
                    "code": 20026,
                },
            )
        ]
    )
    client = FeishuOAuthClient(
        api_base="https://open.feishu.cn",
        app_id="cli_test",
        app_secret="sec_test",
        http_client_factory=fake_http,
    )
    manager = FeishuUserTokenManager(store=store, client=client, refresh_ahead_seconds=0)

    with pytest.raises(FeishuReauthorizationRequired):
        manager.get_valid_access_token("ou_expired")

    row = store.get_feishu_user_token("ou_expired")
    assert row is not None
    assert row["status"] == "reauth_required"
    assert "refresh token invalid" in str(row["last_error"] or "")


def test_oauth_callback_service_serves_feishu_callback(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "memory" / "feishu" / "state.sqlite3")
    fake_http = _FakeSyncHTTPFactory(
        [
            httpx.Response(
                200,
                json={
                    "access_token": "u_access_2",
                    "refresh_token": "u_refresh_2",
                    "token_type": "Bearer",
                    "scope": "task:read",
                    "expires_in": 7200,
                },
            ),
            httpx.Response(200, json={"open_id": "ou_http"}),
        ]
    )
    client = FeishuOAuthClient(
        api_base="https://open.feishu.cn",
        app_id="cli_test",
        app_secret="sec_test",
        http_client_factory=fake_http,
    )
    service = FeishuOAuthService(
        store=store,
        client=client,
        redirect_uri="https://bot.example.com/oauth/feishu/callback",
        scopes=["task:read"],
    )
    callback = OAuthCallbackService(
        host="127.0.0.1",
        port=0,
        callback_path="/oauth/feishu/callback",
        feishu_service=service,
    )

    auth_url = service.create_authorization_url(actor_open_id="ou_sender", chat_id="oc_group")
    state = _extract_state_from_url(auth_url)

    callback.start()
    try:
        server = callback._server
        assert server is not None
        port = int(server.server_port)
        response = httpx.get(
            f"http://127.0.0.1:{port}/oauth/feishu/callback",
            params={"state": state, "code": "code_http"},
            timeout=5.0,
        )
        assert response.status_code == 200
        assert "Authorization Completed" in response.text
    finally:
        callback.stop()
