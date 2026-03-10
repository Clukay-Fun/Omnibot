"""Configuration loading utilities."""

import json
from pathlib import Path
from typing import Any

from nanobot.config.schema import Config


# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    return Path.home() / ".nanobot" / "config.json"


def get_data_dir() -> Path:
    """Get the active instance data directory for backward compatibility."""
    return get_config_path().parent


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)
            config = Config.model_validate(data)
            return config.apply_shared_integration_defaults()
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")

    return Config().apply_shared_integration_defaults()


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.to_persisted_dict()

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _get_or_create(container: dict[str, Any], key: str) -> dict[str, Any]:
    existing = container.get(key)
    if isinstance(existing, dict):
        return existing
    created: dict[str, Any] = {}
    container[key] = created
    return created


def _prefer_new_value(target: dict[str, Any], key: str, incoming: Any, warnings: list[str], source: str) -> None:
    if incoming in (None, "", [], {}):
        return
    existing = target.get(key)
    if existing in (None, "", [], {}):
        target[key] = incoming
        return
    if existing != incoming:
        warnings.append(f"Feishu config keeps new integrations value for {key}, ignores legacy {source}")


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")

    warnings: list[str] = []
    integrations = _get_or_create(data, "integrations")
    feishu = _get_or_create(integrations, "feishu")
    auth = _get_or_create(feishu, "auth")
    api = _get_or_create(feishu, "api")
    bitable = _get_or_create(feishu, "bitable")
    storage = _get_or_create(feishu, "storage")

    channels = _get_or_create(data, "channels")
    feishu_channel = _get_or_create(channels, "feishu")
    feishu_tool = _get_or_create(tools, "feishuData")
    legacy_bitable = _get_or_create(feishu_tool, "bitable")

    for key in ("appId", "appSecret", "encryptKey", "verificationToken"):
        mapped_key = {
            "appId": "appId",
            "appSecret": "appSecret",
            "encryptKey": "encryptKey",
            "verificationToken": "verificationToken",
        }[key]
        _prefer_new_value(auth, mapped_key, feishu_channel.get(key), warnings, f"channels.feishu.{key}")
        _prefer_new_value(auth, mapped_key, feishu_tool.get(key), warnings, f"tools.feishuData.{key}")

    _prefer_new_value(api, "apiBase", feishu_tool.get("apiBase"), warnings, "tools.feishuData.apiBase")

    for key in (
        "stateDbPath",
        "sqliteJournalMode",
        "sqliteSynchronous",
        "sqliteBusyTimeoutMs",
        "sqliteBackupDir",
        "sqliteBackupIntervalHours",
        "sqliteBackupRetentionDays",
    ):
        _prefer_new_value(storage, key, feishu_channel.get(key), warnings, f"channels.feishu.{key}")
        _prefer_new_value(storage, key, feishu_tool.get(key), warnings, f"tools.feishuData.{key}")

    for key in ("domain", "defaultAppToken", "defaultTableId", "defaultViewId", "fieldMapping"):
        _prefer_new_value(bitable, key, legacy_bitable.get(key), warnings, f"tools.feishuData.bitable.{key}")

    if "defaultAppToken" in feishu_tool:
        _prefer_new_value(bitable, "defaultAppToken", feishu_tool.get("defaultAppToken"), warnings, "tools.feishuData.defaultAppToken")
    if "defaultTableId" in feishu_tool:
        _prefer_new_value(bitable, "defaultTableId", feishu_tool.get("defaultTableId"), warnings, "tools.feishuData.defaultTableId")
    if "defaultViewId" in feishu_tool:
        _prefer_new_value(bitable, "defaultViewId", feishu_tool.get("defaultViewId"), warnings, "tools.feishuData.defaultViewId")
    if "fieldMapping" in feishu_tool:
        _prefer_new_value(bitable, "fieldMapping", feishu_tool.get("fieldMapping"), warnings, "tools.feishuData.fieldMapping")

    for warning in warnings:
        print(f"Warning: {warning}")
    return data
