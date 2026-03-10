"""用于创建后台子代理（Subagents）的派生工具。"""

from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.turn_runtime import TurnRuntime


# region [子代理派生工具]

class SpawnTool(Tool):
    """
    用处: 孵化创建新后台子代理的执行入口点。

    功能:
        - 允许当前 Agent (主线程) 将长尾耗时任务“分发”给另一个独立运行的子代理 (Subagent) 去平行执行。
    """

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel = "cli"
        self._origin_chat_id = "direct"
        self._session_key = "cli:direct"

    def set_context(self, channel: str, chat_id: str) -> None:
        """
        用处: 设置子代理执行完毕后向谁报告的联系方式。

        功能:
            - 将当前来源渠道与用户存入缓存实例中，后续组装为 Session Key。
        """
        self._origin_channel = channel
        self._origin_chat_id = chat_id
        self._session_key = f"{channel}:{chat_id}"

    def set_turn_runtime(self, runtime: "TurnRuntime") -> None:
        self._origin_channel = runtime.channel
        self._origin_chat_id = runtime.chat_id
        self._session_key = runtime.session_key or f"{runtime.channel}:{runtime.chat_id}"

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        """
        用处: 对派生请求的意图阐述。

        功能:
            - 告知大模型只有当面临无法在同一轮立刻响应完毕的长尾复杂任务时才主动使用。
        """
        return (
            "派生（Spawn）一个子代理以在后台处理任务。"
            "当任务复杂或耗时较长、且可以独立运行时使用。"
            "子代理将完成任务并在完成后自动报告。"
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
                "mode": {
                    "type": "string",
                    "description": "可选子代理模式，如 subagent_plan 或 subagent_apply。",
                },
                "grant": {
                    "type": "object",
                    "description": "可选显式授权对象，例如 {allowed_tools:[...] }。",
                },
            },
            "required": ["task"],
        }

    async def execute(self, **kwargs: Any) -> str:
        """
        用处: 执行子代理孵化动作。

        功能:
            - 从参数重抓取核心 task 语句并根据授权将请求转发至挂载的 `_manager.spawn` 管理器中。
        """
        task = str(kwargs.get("task") or "")
        label = kwargs.get("label")
        mode = kwargs.get("mode")
        grant = kwargs.get("grant") if isinstance(kwargs.get("grant"), dict) else None
        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=self._origin_channel,
            origin_chat_id=self._origin_chat_id,
            session_key=self._session_key,
            mode=mode or "subagent_plan",
            grant=grant,
        )

# endregion
