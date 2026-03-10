"""
描述: 衍生于主环境上下文的统一文件路径分发层。
主要功能:
    - 将散落的数据落盘诉求（日志/多媒体/会话/工作空间）约束到 `~/.nanobot/` 等指定的相对安全范围中，并自带缺失自愈建立。
"""

from __future__ import annotations

from pathlib import Path

from nanobot.config.loader import get_config_path
from nanobot.utils.helpers import ensure_dir


def get_data_dir() -> Path:
    """
    用处: 获得实例级数据落脚根目录。

    功能:
        - 基于当前的 `config.json` 获取隔离存储层，若无则主动连级补齐创建。
    """
    return ensure_dir(get_config_path().parent)


def get_runtime_subdir(name: str) -> Path:
    """Return a named runtime subdirectory under the instance data dir."""
    return ensure_dir(get_data_dir() / name)


def get_media_dir(channel: str | None = None) -> Path:
    """Return the media directory, optionally namespaced per channel."""
    base = get_runtime_subdir("media")
    return ensure_dir(base / channel) if channel else base


def get_cron_dir() -> Path:
    """Return the cron storage directory."""
    return get_runtime_subdir("cron")


def get_logs_dir() -> Path:
    """Return the logs directory."""
    return get_runtime_subdir("logs")


def get_workspace_path(workspace: str | None = None) -> Path:
    """
    用处: 定位智能体可读取/覆写的授权执行环境目录。

    功能:
        - 若传参为空就取默认 `~/.nanobot/workspace` 主沙盒。它也是本地命令行交互时的大本营基点。
    """
    path = Path(workspace).expanduser() if workspace else Path.home() / ".nanobot" / "workspace"
    return ensure_dir(path)


def get_cli_history_path() -> Path:
    """Return the shared CLI history file path."""
    return Path.home() / ".nanobot" / "history" / "cli_history"


def get_bridge_install_dir() -> Path:
    """Return the shared WhatsApp bridge installation directory."""
    return Path.home() / ".nanobot" / "bridge"


def get_legacy_sessions_dir() -> Path:
    """Return the legacy global session directory used for migration fallback."""
    return Path.home() / ".nanobot" / "sessions"
