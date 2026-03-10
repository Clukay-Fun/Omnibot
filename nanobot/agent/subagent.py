"""
描述: 用于后台任务并行与离线异步执行的子代理（Subagent）管理器。
主要功能:
    - 隔离并管理与会话解耦的长期任务，防止由于多重 API 等待导致单向阻塞。
    - 对内联挂载指定的工具有限开放状态，并将回退事件传递回核心事件总线体系中。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nanobot.config.schema import ExecToolConfig, FeishuDataConfig
    from nanobot.storage.sqlite_store import SQLiteConnectionOptions

from loguru import logger

from nanobot.agent.subagent_contracts import SubagentResultContract, normalize_subagent_contract
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.registry import ToolExposureContext, ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider

# region [子代理管理器]

class SubagentManager:
    """
    用处: 统一的 Subagent 生命周期集装箱。

    功能:
        - 创建新并行的模型调度 Task，维持会话绑定与释放索引，监听退出状态并发送通告回源。
    """

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,
        brave_api_key: str | None = None,
        exec_config: ExecToolConfig | None = None,
        restrict_to_workspace: bool = False,
        feishu_data_config: FeishuDataConfig | None = None,
        state_db_path: Path | None = None,
        sqlite_options: "SQLiteConnectionOptions | None" = None,
    ):
        from nanobot.config.schema import ExecToolConfig
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.restrict_to_workspace = restrict_to_workspace
        self.feishu_data_config = feishu_data_config
        self.state_db_path = state_db_path
        self.sqlite_options = sqlite_options
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        mode: str = "subagent_plan",
        grant: dict[str, Any] | None = None,
    ) -> str:
        """
        用处: 派发并游离出一条独立的 LLM 工作通道。

        功能:
            - 提取工作上下文创建 Task 追踪句柄放入 event loop 池并附着垃圾清理 callback。
        """
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {"channel": origin_channel, "chat_id": origin_chat_id}

        bg_task = asyncio.create_task(
            self._run_subagent(task_id, task, display_label, origin, mode=mode, grant=grant)
        )
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    # endregion

    # region [子代理核心迭代器]

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        mode: str = "subagent_plan",
        grant: dict[str, Any] | None = None,
    ) -> None:
        """
        用处: Subagent 的微型单体 AgentLoop 简化实现。

        功能:
            - 生成专用的工具白名单集合和 Prompt，进行循环 LLM Calling 直到无新工具生成或抛错超时。
        """
        logger.info("Subagent [{}] starting task: {}", task_id, label)

        try:
            # 构建子代理所需工具（不允许嵌套 message 和 spawn 工具）
            tools = ToolRegistry()
            allowed_dir = self.workspace if self.restrict_to_workspace else None
            tools.register(ReadFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(WriteFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(EditFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(ListDirTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
                path_append=self.exec_config.path_append,
            ))
            tools.register(WebSearchTool(api_key=self.brave_api_key))
            tools.register(WebFetchTool())

            if self.feishu_data_config and self.feishu_data_config.enabled:
                from nanobot.agent.tools.feishu_data.registry import build_feishu_data_tools
                for tool in build_feishu_data_tools(
                    self.feishu_data_config,
                    workspace=self.workspace,
                    state_db_path=self.state_db_path,
                    sqlite_options=self.sqlite_options,
                    provider=self.provider,
                    model=self.model,
                ):
                    tools.register(tool)

            allowed_tools = tuple(
                str(item).strip()
                for item in (grant or {}).get("allowed_tools", [])
                if str(item).strip()
            )
            allowed_tables = tuple(
                str(item).strip()
                for item in (grant or {}).get("allowed_tables", [])
                if str(item).strip()
            )
            tool_exposure = ToolExposureContext(
                channel=origin.get("channel", ""),
                user_text=task,
                mode=mode,
                authorized_tools=allowed_tools,
                authorized_resources={"allowed_tables": allowed_tables} if allowed_tables else {},
            )

            system_prompt = self._build_subagent_prompt(mode)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            # 运行内部智能体循环（带迭代次数限制）
            max_iterations = 15
            iteration = 0
            final_result: str | None = None

            while iteration < max_iterations:
                iteration += 1

                response = await self.provider.chat(
                    messages=messages,
                    tools=tools.get_definitions(exposure=tool_exposure),
                    model=self.model,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    reasoning_effort=self.reasoning_effort,
                )

                if response.has_tool_calls:
                    # 带有工具调用结果的助手消息追加
                    tool_call_dicts = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                            },
                        }
                        for tc in response.tool_calls
                    ]
                    messages.append({
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": tool_call_dicts,
                    })

                    # 执行生成的工具
                    for tool_call in response.tool_calls:
                        args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                        logger.debug("Subagent [{}] executing: {} with arguments: {}", task_id, tool_call.name, args_str)
                        result = await tools.execute(tool_call.name, tool_call.arguments, exposure=tool_exposure)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": result,
                        })
                else:
                    final_result = response.content
                    break

            contract = normalize_subagent_contract(final_result, mode=mode, status="ok")

            logger.info("Subagent [{}] completed successfully", task_id)
            await self._announce_result(task_id, label, task, contract, origin, "ok")

        except Exception as e:
            error_msg = f"Error: {str(e)}"
            logger.error("Subagent [{}] failed: {}", task_id, e)
            contract = normalize_subagent_contract(error_msg, mode=mode, status="error")
            await self._announce_result(task_id, label, task, contract, origin, "error")

    # endregion

    # region [子代理通信及辅助]

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        contract: SubagentResultContract,
        origin: dict[str, str],
        status: str,
    ) -> None:
        """
        用处: 向用户发起事件通知。

        功能:
            - 构建一个纯系统级消息结构体推流至原始对话房间模拟系统返回的结果提示气泡。
        """
        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = f"""[Subagent '{label}' {status_text}]

Task: {task}

Structured Result:
{json.dumps(contract.to_payload(), ensure_ascii=False, indent=2)}

Summarize this naturally for the user. Prefer `summary` as the user-facing truth. Keep it brief (1-2 sentences). Do not mention technical details like "subagent" or task IDs."""

        # 作为系统消息注入以触发主智能体执行
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
        )

        await self.bus.publish_inbound(msg)
        logger.debug("Subagent [{}] announced result to {}:{}", task_id, origin['channel'], origin['chat_id'])

    def _build_subagent_prompt(self, mode: str = "subagent_plan") -> str:
        """
        用处: 为子代理约束极简与高纯度的输出 Schema 断言。

        功能:
            - 强制该下挂代理只通过固定格式的 JSON 交卷（Output Contract）而不仅是对话。
        """
        from nanobot.agent.context import ContextBuilder
        from nanobot.agent.skills import SkillsLoader

        time_ctx = ContextBuilder._build_runtime_context(None, None)
        parts = [f"""# Subagent

{time_ctx}

You are a specialist subagent spawned by the main agent to complete a specific task.
Stay focused on the assigned task. Your final response will be reported back to the main agent.

## Specialist Mode
{mode}

## Output Contract
Return ONLY JSON with the following shape:
{{
  "kind": "{mode}",
  "status": "ok|partial|error",
  "summary": "short human-readable finding",
  "data": {{"key": "value"}},
  "confidence": "low|medium|high",
  "next_action": "report|needs_input|apply"
}}

Do not return markdown fences. Do not return free-form prose outside the JSON object.

## Workspace
{self.workspace}"""]

        skills_summary = SkillsLoader(self.workspace).build_skills_summary()
        if skills_summary:
            parts.append(f"## Skills\n\nRead SKILL.md with read_file to use a skill.\n\n{skills_summary}")

        return "\n\n".join(parts)

    async def cancel_by_session(self, session_key: str) -> int:
        """
        用处: 中断正在长久运行或者死循环的后台 Subagent。

        功能:
            - 根据 Session Key 查找到隶属所有协程对象后立刻分发 Cancel 信号。
        """
        tasks = [self._running_tasks[tid] for tid in self._session_tasks.get(session_key, [])
                 if tid in self._running_tasks and not self._running_tasks[tid].done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def get_running_count(self) -> int:
        """
        用处: 监控与心跳检查。

        功能:
            - 总计多少子任务仍在活跃。
        """
        return len(self._running_tasks)

    # endregion
