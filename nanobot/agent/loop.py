"""Agent 循环：核心处理引擎。"""

from __future__ import annotations

import asyncio
import json
import re
import time
import weakref
from contextlib import AsyncExitStack, suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanobot.agent.skill_runtime import (
    EmbeddingSkillRouter,
    OutputGuard,
    SkillSpecExecutor,
    SkillSpecRegistry,
    UserMemoryStore,
)
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.config.schema import (
        ChannelsConfig,
        ExecToolConfig,
        FeishuDataConfig,
        ProviderConfig,
        ResponseTemplateConfig,
        SkillSpecConfig,
    )
    from nanobot.cron.service import CronService


class AgentLoop:
    """
    Agent 循环是核心处理引擎。

    它的工作流：
    1. 从事件总线接收消息
    2. 使用历史记录、记忆、技能构建上下文
    3. 调用大语言模型（LLM）
    4. 执行工具调用
    5. 将响应发送回去
    """

    _TOOL_RESULT_MAX_CHARS = 500
    _ANSWER_PLACEHOLDER_DELAY_MS = 250
    _ANSWER_PLACEHOLDER_TEXT = "🐈努力回答中..."

    # region [初始化与配置]

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 40,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        memory_window: int = 100,
        reasoning_effort: str | None = None,
        brave_api_key: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        feishu_data_config: "FeishuDataConfig | None" = None,
        response_template_config: "ResponseTemplateConfig | None" = None,
        skillspec_config: "SkillSpecConfig | None" = None,
        skillspec_embedding_provider_config: "ProviderConfig | None" = None,
    ):
        from nanobot.agent.response_templates import TemplateRenderer, TemplateRouter
        from nanobot.config.schema import (
            ExecToolConfig,
            ProviderConfig,
            ResponseTemplateConfig,
            SkillSpecConfig,
        )
        self.bus = bus
        self.channels_config = channels_config
        self.feishu_data_config = feishu_data_config
        self.response_template_config = response_template_config or ResponseTemplateConfig()
        self.skillspec_config = skillspec_config or SkillSpecConfig()
        self._skillspec_embedding_provider_config = skillspec_embedding_provider_config or ProviderConfig()
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.reasoning_effort = reasoning_effort
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=reasoning_effort,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
            feishu_data_config=feishu_data_config,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._consolidating: set[str] = set()  # Session keys with consolidation in progress
        self._consolidation_tasks: set[asyncio.Task] = set()  # Strong refs to in-flight tasks
        self._consolidation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._processing_lock = asyncio.Lock()
        self._template_router = TemplateRouter()
        self._template_renderer = TemplateRenderer(self.response_template_config.max_list_items)
        self._stream_warmup_chars = 24
        self._stream_warmup_ms = 300
        if self.channels_config:
            feishu_cfg = getattr(self.channels_config, "feishu", None)
            if feishu_cfg is not None:
                self._stream_warmup_chars = max(1, int(getattr(feishu_cfg, "stream_answer_warmup_chars", 24)))
                self._stream_warmup_ms = max(0, int(getattr(feishu_cfg, "stream_answer_warmup_ms", 300)))
        self._register_default_tools()
        self._skillspec_registry: SkillSpecRegistry | None = None
        self._skillspec_runtime: SkillSpecExecutor | None = None
        self._init_skillspec_runtime()

    def _init_skillspec_runtime(self) -> None:
        if not self.skillspec_config.enabled:
            return

        workspace_root = self.workspace / "skillspec"
        if not self.skillspec_config.workspace_override_enabled:
            workspace_root = self.workspace / "__skillspec_disabled__"

        self._skillspec_registry = SkillSpecRegistry(workspace_root=workspace_root)
        self._skillspec_registry.load()
        embedding_router = EmbeddingSkillRouter(
            embedding_enabled=self.skillspec_config.embedding_enabled,
            embedding_top_k=self.skillspec_config.embedding_top_k,
            embedding_model=self.skillspec_config.embedding_model,
            embedding_timeout_seconds=self.skillspec_config.embedding_timeout_seconds,
            embedding_cache_ttl_seconds=self.skillspec_config.embedding_cache_ttl_seconds,
            provider_config=self._skillspec_embedding_provider_config,
        )
        self._skillspec_runtime = SkillSpecExecutor(
            registry=self._skillspec_registry,
            tools=self.tools,
            output_guard=OutputGuard(),
            user_memory=UserMemoryStore(self.workspace),
            embedding_router=embedding_router,
        )

        if self.skillspec_config.startup_report_enabled:
            report = self._skillspec_registry.report
            logger.info(
                "Skillspec registry loaded={} overridden={} collisions={} disabled={}",
                len(report.loaded),
                len(report.overridden),
                len(report.source_collisions),
                len(report.disabled),
            )
            if report.source_collisions:
                logger.info("Skillspec source collisions: {}", "; ".join(report.source_collisions))
            if self.skillspec_config.startup_report_include_invalid and report.invalid:
                logger.warning("Skillspec invalid entries: {}", "; ".join(report.invalid))

    def _register_default_tools(self) -> None:
        """注册默认工具集。"""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
            path_append=self.exec_config.path_append,
        ))
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

        if self.feishu_data_config and self.feishu_data_config.enabled:
            from nanobot.agent.tools.feishu_data.registry import build_feishu_data_tools
            for tool in build_feishu_data_tools(self.feishu_data_config):
                self.tools.register(tool)

    # endregion

    # region [MCP 服务配置与清理]

    async def _connect_mcp(self) -> None:
        """连接到已配置的 MCP 服务器（单次、懒加载）。"""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except Exception as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    # endregion

    # region [工具与执行辅助方法]

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """为所有需要路由信息的工具更新上下文。"""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """移除某些模型嵌入在内容中的 <think>…</think> 块。"""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """将工具调用格式化为简明的提示，例如 'web_search("query")'。"""
        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    @staticmethod
    def _extract_think(text: str | None) -> str | None:
        """提取内容中的 <think>…</think> 文本。"""
        if not text:
            return None
        matches = re.findall(r"<think>([\s\S]*?)</think>", text)
        if not matches:
            return None
        joined = "\n".join(m.strip() for m in matches if m and m.strip())
        return joined or None

    @staticmethod
    def _extract_thinking_blocks_text(thinking_blocks: list[dict] | None) -> str | None:
        """从 thinking_blocks 中提取可读文本。"""
        if not thinking_blocks:
            return None

        parts: list[str] = []

        def _collect(value: Any) -> None:
            if isinstance(value, str):
                text = value.strip()
                if text:
                    parts.append(text)
                return
            if isinstance(value, dict):
                for key in ("text", "content", "thinking", "summary"):
                    if key in value:
                        _collect(value[key])
                return
            if isinstance(value, list):
                for item in value:
                    _collect(item)

        _collect(thinking_blocks)
        if not parts:
            return None
        return "\n".join(parts)

    @staticmethod
    def _short_text(value: Any, limit: int = 240) -> str:
        """将任意值压缩为单行短文本。"""
        if value is None:
            return ""
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        return compact[:limit].rstrip() + "..."

    def _build_thinking_detail(self, response: LLMResponse) -> str | None:
        """优先提取可展示的思考细节。"""
        for candidate in (
            self._extract_think(response.content),
            self._extract_thinking_blocks_text(response.thinking_blocks),
            (response.reasoning_content or "").strip() or None,
        ):
            if candidate:
                return candidate
        return None

    def _render_with_template(
        self,
        *,
        user_text: str,
        fallback_text: str | None,
        turn_messages: list[dict],
        timings: list[dict[str, int | str | bool]],
        total_ms: int,
    ) -> str | None:
        if not self.response_template_config.enabled:
            return fallback_text

        from nanobot.agent.response_templates import (
            build_audit_summary,
            collect_tool_payloads,
            uses_structured_feishu_data,
        )

        payloads = collect_tool_payloads(turn_messages)
        if not payloads or not uses_structured_feishu_data(payloads):
            return fallback_text

        decision = self._template_router.route(user_text=user_text, payloads=payloads)
        if decision.template_id == "chat.PLAIN":
            return fallback_text

        if not self.response_template_config.strict_mode and decision.template_id == "generic.SUMMARY":
            return fallback_text

        rendered = self._template_renderer.render(decision=decision, payloads=payloads, fallback_text=fallback_text)
        audit = build_audit_summary(decision=decision, payloads=payloads, timings=timings, total_ms=total_ms)
        if self.response_template_config.log_audit_summary:
            logger.info("Template audit: {}", audit.replace("\n", " | "))

        if not self.response_template_config.show_audit_summary:
            return rendered

        return f"{audit}\n\n{rendered}"

    # endregion

    # region [核心调度与执行循环]

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
    ) -> tuple[str | None, list[str], list[dict], list[dict[str, int | str | bool]]]:
        """运行智能体迭代循环。返回 (final_content, tools_used, messages)。"""
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        timings: list[dict[str, int | str | bool]] = []
        thinking_done_sent = False
        thinking_detail_emitted = False

        async def _emit_progress(content: str, **kwargs: Any) -> None:
            if not on_progress:
                return
            try:
                await on_progress(content, **kwargs)
            except TypeError:
                await on_progress(content)

        while iteration < self.max_iterations:
            iteration += 1
            streamed_content = ""
            published_stream = False
            stream_started_at = time.monotonic()
            announced_tool_names: set[str] = set()
            answer_placeholder_task: asyncio.Task[None] | None = None

            async def _on_delta(delta: str) -> None:
                nonlocal streamed_content, published_stream
                if not on_progress or not delta:
                    return
                streamed_content += delta

                if not published_stream and iteration == 1:
                    elapsed_ms = int((time.monotonic() - stream_started_at) * 1000)
                    if len(streamed_content) < self._stream_warmup_chars or elapsed_ms < self._stream_warmup_ms:
                        return

                await _emit_progress(streamed_content, phase="answer")
                published_stream = True

            async def _on_tool_call_name(tool_name: str) -> None:
                nonlocal thinking_detail_emitted
                name = (tool_name or "").strip()
                if not on_progress or not name or name in announced_tool_names:
                    return
                announced_tool_names.add(name)
                await _emit_progress(f"准备调用 {name}", phase="thinking")
                thinking_detail_emitted = True

            async def _emit_answer_placeholder() -> None:
                if not on_progress:
                    return
                await asyncio.sleep(self._ANSWER_PLACEHOLDER_DELAY_MS / 1000)
                if published_stream or thinking_detail_emitted:
                    return
                await _emit_progress(self._ANSWER_PLACEHOLDER_TEXT, phase="answer")

            if on_progress and iteration == 1:
                answer_placeholder_task = asyncio.create_task(_emit_answer_placeholder())

            llm_started = time.perf_counter()
            try:
                response = await self.provider.chat(
                    messages=messages,
                    tools=self.tools.get_definitions(),
                    model=self.model,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    reasoning_effort=self.reasoning_effort,
                    on_delta=_on_delta if on_progress else None,
                    on_tool_call_name=_on_tool_call_name if on_progress else None,
                )
            finally:
                if answer_placeholder_task and not answer_placeholder_task.done():
                    answer_placeholder_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await answer_placeholder_task
            timings.append({
                "stage": f"llm:{iteration}",
                "duration_ms": int((time.perf_counter() - llm_started) * 1000),
            })

            if response.has_tool_calls:
                thinking_detail = self._build_thinking_detail(response)
                if thinking_detail:
                    await _emit_progress(thinking_detail, phase="thinking")
                    thinking_detail_emitted = True

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                    args_preview = self._short_text(tool_call.arguments, limit=160)
                    if args_preview:
                        await _emit_progress(f"调用 {tool_call.name}，参数：{args_preview}", phase="thinking")
                        thinking_detail_emitted = True
                    else:
                        await _emit_progress(f"正在调用 {tool_call.name} ...", phase="thinking")
                    tool_started = time.perf_counter()
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    tool_duration_ms = int((time.perf_counter() - tool_started) * 1000)
                    timings.append({
                        "stage": f"tool:{tool_call.name}",
                        "duration_ms": tool_duration_ms,
                    })
                    result_preview = self._short_text(result, limit=200)
                    if result_preview:
                        await _emit_progress(f"{tool_call.name} 结果：{result_preview}", phase="thinking")
                        thinking_detail_emitted = True
                    else:
                        await _emit_progress(f"{tool_call.name} 完成，继续思考中...", phase="thinking")
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )

                await _emit_progress("已获取数据，正在整理答案...", phase="thinking")
                thinking_detail_emitted = True
            else:
                clean = self._strip_think(response.content)
                # Don't persist error responses to session history — they can
                # poison the context and cause permanent 400 loops (#1303).
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    break

                if on_progress and not thinking_done_sent and thinking_detail_emitted:
                    await _emit_progress("思考完成", phase="thinking_done")
                    thinking_done_sent = True

                if on_progress and clean and not published_stream:
                    await _emit_progress(clean, phase="answer")

                messages = self.context.add_assistant_message(
                    messages, clean, reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                final_content = clean
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        return final_content, tools_used, messages, timings

    async def run(self) -> None:
        """运行智能体循环，将消息分发为任务，以保持对 /stop 命令的响应能力。"""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if msg.content.strip().lower() == "/stop":
                await self._handle_stop(msg)
            else:
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(lambda t, k=msg.session_key: self._active_tasks.get(k, []) and self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None)

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """取消会话的所有活动任务和子代理（subagents）。"""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        total = cancelled + sub_cancelled
        content = f"⏹ Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    async def _dispatch(self, msg: InboundMessage) -> None:
        """在全局锁下处理一条消息。"""
        async with self._processing_lock:
            try:
                response = await self._process_message(msg)
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))

    async def close_mcp(self) -> None:
        """关闭 MCP 连接。"""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def stop(self) -> None:
        """停止智能体主循环。"""
        self._running = False
        logger.info("Agent loop stopping")

    # endregion

    # region [消息处理核心逻辑]

    @staticmethod
    def _build_commands_help_text() -> str:
        """返回命令总览（简短说明）。"""
        return (
            "🐈 可用指令\n\n"
            "- /help 或 /commands：显示全部指令说明\n"
            "- /new：归档并清空当前会话\n"
            "- /stop：停止当前会话中的进行中任务\n"
            "- /session：查看会话子命令\n"
            "- /session new [标题]：创建飞书话题会话\n"
            "- /session list：列出当前聊天下的会话\n"
            "- /session del [id|main]：删除指定会话"
        )

    def _list_chat_session_keys(self, channel: str, chat_id: str, current_key: str) -> list[str]:
        """列出当前聊天上下文下的会话 keys（主会话 + 线程会话）。"""
        base_key = f"{channel}:{chat_id}"
        keys = {base_key, current_key}
        for item in self.sessions.list_sessions():
            key = str(item.get("key") or "")
            if key == base_key or key.startswith(f"{base_key}:"):
                keys.add(key)
        return sorted(keys, key=lambda x: (0 if x == base_key else 1, x))

    def _handle_session_command(self, msg: InboundMessage, current_key: str, raw_cmd: str) -> OutboundMessage:
        """处理 /session 子命令。"""
        parts = raw_cmd.split()
        sub = parts[1].lower() if len(parts) > 1 else ""
        base_key = f"{msg.channel}:{msg.chat_id}"

        if sub in ("", "help"):
            content = (
                "会话子命令：\n\n"
                "- /session new [标题]：创建飞书话题会话（缺省为 会话-YYYYMMDD-HHMM）\n"
                "- /session list：列出当前聊天下的会话\n"
                "- /session del [id|main]：删除当前/指定会话"
            )
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)

        if sub == "new":
            if msg.channel != "feishu":
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="/session new 目前仅支持飞书频道。",
                )

            prefix = "/session new"
            title = raw_cmd[len(prefix):].strip() if raw_cmd.lower().startswith(prefix) else ""
            if not title:
                title = datetime.now().strftime("会话-%Y%m%d-%H%M")

            meta = dict(msg.metadata or {})
            meta["_start_topic_session"] = True
            meta["_reply_in_thread"] = True
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=title,
                metadata=meta,
            )

        session_keys = self._list_chat_session_keys(msg.channel, msg.chat_id, current_key)

        if sub == "list":
            lines = ["当前聊天会话列表："]
            for idx, key in enumerate(session_keys, start=1):
                if key == base_key:
                    label = "main（主会话）"
                else:
                    label = key.removeprefix(f"{base_key}:")
                marker = "（当前）" if key == current_key else ""
                lines.append(f"{idx}. {label}{marker}")
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="\n".join(lines),
            )

        if sub in ("del", "delete", "rm"):
            target_arg = parts[2] if len(parts) > 2 else "current"
            target_key = current_key

            if target_arg == "main":
                target_key = base_key
            elif target_arg.isdigit():
                idx = int(target_arg)
                if idx < 1 or idx > len(session_keys):
                    return OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"会话序号无效：{target_arg}",
                    )
                target_key = session_keys[idx - 1]
            elif target_arg != "current":
                if target_arg.startswith(f"{msg.channel}:"):
                    target_key = target_arg
                elif target_arg.startswith(f"{base_key}:"):
                    target_key = target_arg
                else:
                    target_key = f"{base_key}:{target_arg}"

            deleted = self.sessions.delete(target_key)
            if not deleted:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"未找到会话：{target_key}",
                )
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"已删除会话：{target_key}",
            )

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=f"未知会话子命令：{sub}。请输入 /session 查看帮助。",
        )

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """处理单条传入消息并返回响应。"""
        # 系统消息：从 chat_id 中解析来源 ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = session.get_history(max_messages=self.memory_window)
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
            )
            turn_started = time.perf_counter()
            final_content, _, all_msgs, timings = await self._run_agent_loop(messages)
            turn_new = all_msgs[1 + len(history):]
            total_ms = int((time.perf_counter() - turn_started) * 1000)
            final_content = self._render_with_template(
                user_text=msg.content,
                fallback_text=final_content,
                turn_messages=turn_new,
                timings=timings,
                total_ms=total_ms,
            )
            self._save_turn(session, all_msgs, 1 + len(history))
            self.sessions.save(session)
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)

        # 斜杠命令 (Slash commands)
        raw_cmd = msg.content.strip()
        cmd = raw_cmd.lower()
        if cmd in {"/help", "/commands"}:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self._build_commands_help_text(),
            )
        if cmd.startswith("/session"):
            return self._handle_session_command(msg, key, raw_cmd)
        if cmd == "/new":
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())
            self._consolidating.add(session.key)
            try:
                async with lock:
                    snapshot = session.messages[session.last_consolidated:]
                    if snapshot:
                        temp = Session(key=session.key)
                        temp.messages = list(snapshot)
                        if not await self._consolidate_memory(temp, archive_all=True):
                            return OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="Memory archival failed, session not cleared. Please try again.",
                            )
            except Exception:
                logger.exception("/new archival failed for {}", session.key)
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Memory archival failed, session not cleared. Please try again.",
                )
            finally:
                self._consolidating.discard(session.key)

            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started.")

        if self._skillspec_runtime and self._skillspec_runtime.can_handle_continuation(msg.content):
            continuation = self._skillspec_runtime.continue_from_session(session)
            if continuation is not None and continuation.handled:
                self.sessions.save(session)
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=continuation.content,
                    metadata={**(msg.metadata or {}), "_tool_turn": continuation.tool_turn},
                )

        if self._skillspec_runtime:
            skillspec_result = await self._skillspec_runtime.execute_if_matched(msg, session)
            if skillspec_result.handled:
                self.sessions.save(session)
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=skillspec_result.content,
                    metadata={**(msg.metadata or {}), "_tool_turn": skillspec_result.tool_turn},
                )

        unconsolidated = len(session.messages) - session.last_consolidated
        if (unconsolidated >= self.memory_window and session.key not in self._consolidating):
            self._consolidating.add(session.key)
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())

            async def _consolidate_and_unlock():
                try:
                    async with lock:
                        await self._consolidate_memory(session)
                finally:
                    self._consolidating.discard(session.key)
                    _task = asyncio.current_task()
                    if _task is not None:
                        self._consolidation_tasks.discard(_task)

            _task = asyncio.create_task(_consolidate_and_unlock())
            self._consolidation_tasks.add(_task)

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=self.memory_window)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
        )

        async def _bus_progress(content: str, *, phase: str = "answer") -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_progress_phase"] = phase
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        turn_started = time.perf_counter()
        final_content, tools_used, all_msgs, timings = await self._run_agent_loop(
            initial_messages, on_progress=on_progress or _bus_progress,
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        turn_new = all_msgs[1 + len(history):]
        total_ms = int((time.perf_counter() - turn_started) * 1000)
        final_content = self._render_with_template(
            user_text=msg.content,
            fallback_text=final_content,
            turn_messages=turn_new,
            timings=timings,
            total_ms=total_ms,
        )

        final_text = final_content or "I've completed processing but have no response to give."

        self._save_turn(session, all_msgs, 1 + len(history))
        self.sessions.save(session)

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        preview = final_text[:120] + "..." if len(final_text) > 120 else final_text
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        meta = dict(msg.metadata or {})
        meta["_tool_turn"] = bool(tools_used)

        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_text,
            metadata=meta,
        )

    # endregion

    # region [记忆与状态管理]

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """将会话的新一轮消息保存起来，截断过长的工具结果。"""
        from datetime import datetime
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # 跳过空的助手消息 —— 它们会污染会话上下文
            if role == "tool" and isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                entry["content"] = content[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    continue
                if isinstance(content, list):
                    entry["content"] = [
                        {"type": "text", "text": "[image]"} if (
                            c.get("type") == "image_url"
                            and c.get("image_url", {}).get("url", "").startswith("data:image/")
                        ) else c for c in content
                    ]
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    async def _consolidate_memory(self, session, archive_all: bool = False) -> bool:
        """委托给 MemoryStore.consolidate()，成功时返回 True。"""
        return await MemoryStore(self.workspace).consolidate(
            session, self.provider, self.model,
            archive_all=archive_all, memory_window=self.memory_window,
        )

    # endregion

    # region [直接调用接口]

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """直接处理一条消息（用于 CLI 或 cron 定时任务）。"""
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        response = await self._process_message(msg, session_key=session_key, on_progress=on_progress)
        return response.content if response else ""

    # endregion
