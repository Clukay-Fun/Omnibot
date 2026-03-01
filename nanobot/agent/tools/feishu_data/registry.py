"""飞书工具注册工厂：组装配置和 Client 以初始化所有 Feishu 数据工具。"""

from typing import Iterable

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.feishu_data.bitable import BitableSearchTool
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
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

    tools = [
        BitableSearchTool(config, client),
    ]

    return tools

# endregion
