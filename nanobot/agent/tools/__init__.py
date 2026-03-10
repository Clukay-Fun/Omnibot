"""
描述: 智能体工具基建模块。
主要功能:
    - 导出工具基类 `Tool` 与工具注册表 `ToolRegistry`，供各具体能力模块继承与挂载。
"""

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry

__all__ = ["Tool", "ToolRegistry"]
