"""用于创建后台子代理（Subagents）的派生工具。"""

from typing import Any, TYPE_CHECKING

from nanobot.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager


# region [子代理派生工具]

class SpawnTool(Tool):
    """用于派生后台子代理执行任务的工具。"""
    
    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel = "cli"
        self._origin_chat_id = "direct"
        self._session_key = "cli:direct"
    
    def set_context(self, channel: str, chat_id: str) -> None:
        """设置子代理向最初来源通报结果的上下文。"""
        self._origin_channel = channel
        self._origin_chat_id = chat_id
        self._session_key = f"{channel}:{chat_id}"
    
    @property
    def name(self) -> str:
        return "spawn"
    
    @property
    def description(self) -> str:
        return (
            "派生（Spawn）一个子代理以在后台处理任务。"
            "当任务复杂或耗时较长、且可以独立运行时使用。"
            "子代理将完成任务并在完成后报告。"
        )
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "需要子代理完成的任务",
                },
                "label": {
                    "type": "string",
                    "description": "可选的任务简短标签（用于显示）",
                },
            },
            "required": ["task"],
        }
    
    async def execute(self, task: str, label: str | None = None, **kwargs: Any) -> str:
        """派生子代理在后台执行指定的任务。"""
        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=self._origin_channel,
            origin_chat_id=self._origin_chat_id,
            session_key=self._session_key,
        )

# endregion
