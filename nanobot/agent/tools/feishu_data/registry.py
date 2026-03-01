"""飞书工具注册工厂：组装配置和 Client 以初始化所有 Feishu 数据工具。"""

from typing import Iterable

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.feishu_data.bitable import (
    BitableGetTool,
    BitableListTablesTool,
    BitableSearchPersonTool,
    BitableSearchTool,
)
from nanobot.agent.tools.feishu_data.bitable_write import (
    BitableCreateTool,
    BitableDeleteTool,
    BitableUpdateTool,
)
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.confirm_store import ConfirmTokenStore
from nanobot.agent.tools.feishu_data.doc_search import DocSearchTool
from nanobot.config.schema import FeishuDataConfig

# region [注册工厂]


def build_feishu_data_tools(config: FeishuDataConfig) -> Iterable[Tool]:
    """
    组装并返回所有已启用的飞书数据操作工具。
    在循环引擎或子代理工具初始化时被调用。
    """
    if not config.enabled:
        return []

    client = FeishuDataClient(config)
    confirm_store = ConfirmTokenStore(ttl_seconds=config.confirm_token_ttl_seconds)

    tools: list[Tool] = [
        # 只读工具
        BitableSearchTool(config, client),
        BitableListTablesTool(config, client),
        BitableGetTool(config, client),
        BitableSearchPersonTool(config, client),
        DocSearchTool(config, client),
        # 写入工具（两阶段安全）
        BitableCreateTool(config, client, confirm_store),
        BitableUpdateTool(config, client, confirm_store),
        BitableDeleteTool(config, client, confirm_store),
    ]

    return tools

# endregion
