"""用于动态工具管理的工具注册表。"""

from typing import Any

from nanobot.agent.tools.base import Tool


# region [工具注册表类]

class ToolRegistry:
    """
    智能体工具的注册表。
    
    允许动态注册和执行工具。
    """
    
    def __init__(self):
        self._tools: dict[str, Tool] = {}
    
    def register(self, tool: Tool) -> None:
        """注册一个工具。"""
        self._tools[tool.name] = tool
    
    def unregister(self, name: str) -> None:
        """通过名称注销一个工具。"""
        self._tools.pop(name, None)
    
    def get(self, name: str) -> Tool | None:
        """通过名称获取一个工具。"""
        return self._tools.get(name)
    
    def has(self, name: str) -> bool:
        """检查工具是否已注册。"""
        return name in self._tools
    
    def get_definitions(self) -> list[dict[str, Any]]:
        """获取 OpenAI 格式的所有工具定义。"""
        return [tool.to_schema() for tool in self._tools.values()]
    
    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """使用给定参数执行指定名称的工具。"""
        _HINT = "\n\n[Analyze the error above and try a different approach.]"

        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"

        try:
            errors = tool.validate_params(params)
            if errors:
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + _HINT
            result = await tool.execute(**params)
            if isinstance(result, str) and result.startswith("Error"):
                return result + _HINT
            return result
        except Exception as e:
            return f"Error executing {name}: {str(e)}" + _HINT
    
    @property
    def tool_names(self) -> list[str]:
        """获取已注册的工具名称列表。"""
        return list(self._tools.keys())
    
    def __len__(self) -> int:
        return len(self._tools)
    
    def __contains__(self, name: str) -> bool:
        return name in self._tools

# endregion
