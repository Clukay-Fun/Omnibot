"""飞书工具注册工厂：组装配置和 Client 以初始化所有 Feishu 数据工具。"""

from pathlib import Path
from typing import Iterable

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.feishu_data.bitable import (
    BitableGetTool,
    BitableListFieldsTool,
    BitableMatchTableTool,
    BitableListTablesTool,
    BitablePrepareCreateTool,
    BitableSearchPersonTool,
    BitableSearchTool,
    BitableSyncSchemaTool,
)
from nanobot.agent.tools.feishu_data.bitable_admin_tools import (
    BitableAppCreateTool,
    BitableTableCreateTool,
    BitableViewCreateTool,
)
from nanobot.agent.tools.feishu_data.bitable_write import (
    BitableCreateTool,
    BitableDeleteTool,
    BitableUpdateTool,
)
from nanobot.agent.tools.feishu_data.calendar_tools import (
    CalendarCreateTool,
    CalendarDeleteTool,
    CalendarFreebusyTool,
    CalendarListTool,
    CalendarUpdateTool,
)
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.confirm_store import ConfirmTokenStore
from nanobot.agent.tools.feishu_data.doc_search import DocSearchTool
from nanobot.agent.tools.feishu_data.message_history import MessageHistoryListTool
from nanobot.agent.tools.feishu_data.task_tools import (
    SubtaskCreateTool,
    TaskCommentAddTool,
    TaskCreateTool,
    TaskDeleteTool,
    TaskGetTool,
    TasklistListTool,
    TaskListTool,
    TaskUpdateTool,
)
from nanobot.agent.tools.feishu_data.token_manager import TenantAccessTokenManager
from nanobot.config.schema import FeishuDataConfig
from nanobot.oauth import FeishuOAuthClient, FeishuUserTokenManager
from nanobot.storage.sqlite_store import SQLiteConnectionOptions, SQLiteStore
from nanobot.utils.helpers import get_state_path, migrate_legacy_path

# region [注册工厂]


def build_feishu_data_tools(
    config: FeishuDataConfig,
    *,
    workspace: Path | None = None,
    state_db_path: Path | None = None,
    sqlite_options: SQLiteConnectionOptions | None = None,
) -> Iterable[Tool]:
    """
    组装并返回所有已启用的飞书数据操作工具。
    在循环引擎或子代理工具初始化时被调用。
    """
    if not config.enabled:
        return []

    token_manager: TenantAccessTokenManager
    sqlite_store: SQLiteStore | None = None
    user_token_manager: FeishuUserTokenManager | None = None
    sqlite_path = state_db_path
    if sqlite_path is None and workspace is not None:
        sqlite_path = get_state_path() / "feishu" / "state.sqlite3"
        migrate_legacy_path(
            workspace / "memory" / "feishu" / "state.sqlite3",
            sqlite_path,
            related_suffixes=("-wal", "-shm", ".bak"),
        )
    if sqlite_path is not None:
        try:
            sqlite_store = SQLiteStore(sqlite_path, options=sqlite_options)
        except Exception as exc:
            logger.warning(f"Failed to initialize Feishu token sqlite store, fallback to memory mode: {exc}")

    if sqlite_store is not None and config.app_id and config.app_secret:
        try:
            oauth_client = FeishuOAuthClient(
                api_base=config.api_base,
                app_id=config.app_id,
                app_secret=config.app_secret,
            )
            user_token_manager = FeishuUserTokenManager(
                store=sqlite_store,
                client=oauth_client,
                refresh_ahead_seconds=config.token.refresh_ahead_seconds,
            )
        except Exception as exc:
            logger.warning(f"Failed to initialize Feishu OAuth user token manager: {exc}")

    token_manager = TenantAccessTokenManager(config=config, sqlite_store=sqlite_store)
    client = FeishuDataClient(config, token_manager=token_manager)
    confirm_store = ConfirmTokenStore(ttl_seconds=config.confirm_token_ttl_seconds)

    flags = config.feature_flags
    tools: list[Tool] = [
        # 只读工具
        BitableSearchTool(config, client),
        BitableListTablesTool(config, client),
        BitableMatchTableTool(config, client),
        BitableListFieldsTool(config, client),
        BitablePrepareCreateTool(config, client),
        BitableSyncSchemaTool(config, client, workspace=workspace),
        BitableGetTool(config, client),
        BitableSearchPersonTool(config, client),
        DocSearchTool(config, client),
        # 写入工具（两阶段安全）
        BitableCreateTool(config, client, confirm_store, workspace=workspace),
        BitableUpdateTool(config, client, confirm_store, workspace=workspace),
        BitableDeleteTool(config, client, confirm_store, workspace=workspace),
    ]

    if flags.bitable_admin_enabled:
        tools.extend(
            [
                BitableAppCreateTool(config, client),
                BitableTableCreateTool(config, client),
                BitableViewCreateTool(config, client),
            ]
        )
    if flags.calendar_enabled:
        tools.extend(
            [
                CalendarListTool(config, client),
                CalendarCreateTool(config, client),
                CalendarUpdateTool(config, client),
                CalendarDeleteTool(config, client),
                CalendarFreebusyTool(config, client),
            ]
        )
    if flags.task_enabled:
        tools.extend(
            [
                TaskCreateTool(config, client),
                TaskGetTool(config, client),
                TaskUpdateTool(config, client),
                TaskDeleteTool(config, client),
                TaskListTool(config, client),
                TasklistListTool(config, client),
                SubtaskCreateTool(config, client),
                TaskCommentAddTool(config, client),
            ]
        )
    if flags.message_history_enabled:
        tools.append(MessageHistoryListTool(config, client, user_token_manager=user_token_manager))

    return tools

# endregion
