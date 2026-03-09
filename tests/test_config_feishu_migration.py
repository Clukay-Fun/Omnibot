import json

from nanobot.config.loader import load_config, save_config


def test_feishu_config_migration_and_shared_fallback(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "channels": {
                    "feishu": {
                        "enabled": True,
                        "appId": "cli-app",
                        "appSecret": "cli-secret",
                    }
                },
                "tools": {
                    "feishuData": {
                        "enabled": True,
                        "apiBase": "https://example.invalid/open-apis",
                        "bitable": {
                            "defaultAppToken": "app-token",
                            "defaultTableId": "tbl-1",
                        },
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.integrations.feishu.auth.app_id == "cli-app"
    assert config.integrations.feishu.auth.app_secret == "cli-secret"
    assert config.integrations.feishu.api.api_base == "https://example.invalid/open-apis"
    assert config.integrations.feishu.bitable.default_app_token == "app-token"
    assert config.integrations.feishu.bitable.default_table_id == "tbl-1"
    assert config.channels.feishu.app_id == "cli-app"
    assert config.tools.feishu_data.app_id == "cli-app"
    assert config.tools.feishu_data.api_base == "https://example.invalid/open-apis"


def test_save_config_omits_duplicated_legacy_feishu_fields(tmp_path):
    config_path = tmp_path / "config.json"
    config = load_config(config_path)
    config.channels.feishu.enabled = True
    config.integrations.feishu.auth.app_id = "shared-app"
    config.integrations.feishu.auth.app_secret = "shared-secret"
    config.integrations.feishu.api.api_base = "https://example.invalid/open-apis"
    config.integrations.feishu.bitable.default_app_token = "app-token"
    config.integrations.feishu.bitable.default_table_id = "tbl-1"
    config.apply_shared_integration_defaults()

    save_config(config, config_path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))

    assert payload["integrations"]["feishu"]["auth"]["appId"] == "shared-app"
    assert payload["integrations"]["feishu"]["bitable"]["defaultTableId"] == "tbl-1"
    assert "appId" not in payload["channels"]["feishu"]
    feishu_data_payload = payload.get("tools", {}).get("feishuData", {})
    assert "appId" not in feishu_data_payload
    assert "apiBase" not in feishu_data_payload


def test_load_config_does_not_write_default_file_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    load_config(None)

    config_path = tmp_path / ".nanobot" / "config.json"
    assert not config_path.exists()


def test_load_config_does_not_persist_env_backed_feishu_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NANOBOT_INTEGRATIONS__FEISHU__AUTH__APP_SECRET", "env-secret")

    config = load_config(None)

    config_path = tmp_path / ".nanobot" / "config.json"
    assert not config_path.exists()
    assert config.integrations.feishu.auth.app_secret == "env-secret"
