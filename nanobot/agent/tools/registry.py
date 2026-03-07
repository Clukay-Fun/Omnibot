"""描述:
主要功能:
    - 维护工具注册、查询与执行入口。
"""

from typing import Any

from nanobot.agent.tools.base import Tool

#region 工具注册表

class ToolRegistry:
    """用处，参数

    功能:
        - 管理工具生命周期并统一执行校验。
    """

    def __init__(self):
        """用处，参数

        功能:
            - 初始化空的工具映射表。
        """
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """用处，参数

        功能:
            - 按工具名称注册工具实例。
        """
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """用处，参数

        功能:
            - 按名称移除已注册工具。
        """
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """用处，参数

        功能:
            - 返回指定名称的工具实例。
        """
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """用处，参数

        功能:
            - 判断工具名称是否存在。
        """
        return name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        """用处，参数

        功能:
            - 生成所有工具的 schema 定义列表。
        """
        return [tool.to_schema() for tool in self._tools.values()]

    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """用处，参数

        功能:
            - 校验参数并执行目标工具，返回文本结果。
        """
        _hint = "\n\n[Analyze the error above and try a different approach.]"

        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"

        try:
            errors = tool.validate_params(params)
            if errors:
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + _hint
            result = await tool.execute(**params)
            if isinstance(result, str) and result.startswith("Error"):
                return result + _hint
            return result
        except Exception as e:
            return f"Error executing {name}: {str(e)}" + _hint

    @property
    def tool_names(self) -> list[str]:
        """用处，参数

        功能:
            - 返回当前所有已注册工具名。
        """
        return list(self._tools.keys())

    def __len__(self) -> int:
        """用处，参数

        功能:
            - 返回注册表中工具数量。
        """
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        """用处，参数

        功能:
            - 支持使用 in 判断工具是否存在。
        """
        return name in self._tools

#endregion
