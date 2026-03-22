"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import uuid
import weakref
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanobot.agent.overlay import OverlayContext
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool, _resolve_path
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.agent.worklog import WorklogStore
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.heartbeat.types import HeartbeatExecutionError, HeartbeatExecutionResult
from nanobot.feishu.streaming import is_framework_delayed_text
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, ExecToolConfig
    from nanobot.cron.service import CronService


class _RestrictedRepairPathMixin:
    """Restrict file tools to a small fixed allowlist within one overlay root."""

    def __init__(self, *, workspace_root: Path, allowed_relative_paths: tuple[str, ...]):
        self._repair_workspace_root = workspace_root.resolve()
        self._repair_allowed_paths = {
            (self._repair_workspace_root / relative).resolve()
            for relative in allowed_relative_paths
        }
        self._repair_allowed_display = ", ".join(allowed_relative_paths)

    def _validate_repair_path(self, path: str) -> None:
        resolved = _resolve_path(
            path,
            workspace=self._repair_workspace_root,
            allowed_dir=self._repair_workspace_root,
        )
        if resolved not in self._repair_allowed_paths:
            raise PermissionError(
                "Path is outside the Feishu DM repair allowlist. "
                f"Allowed files: {self._repair_allowed_display}"
            )


class _RestrictedRepairReadFileTool(_RestrictedRepairPathMixin, ReadFileTool):
    def __init__(self, *, workspace_root: Path, allowed_relative_paths: tuple[str, ...]):
        ReadFileTool.__init__(self, workspace=workspace_root, allowed_dir=workspace_root)
        _RestrictedRepairPathMixin.__init__(
            self,
            workspace_root=workspace_root,
            allowed_relative_paths=allowed_relative_paths,
        )

    async def execute(self, path: str, **kwargs):
        self._validate_repair_path(path)
        return await super().execute(path=path, **kwargs)


class _RestrictedRepairWriteFileTool(_RestrictedRepairPathMixin, WriteFileTool):
    def __init__(self, *, workspace_root: Path, allowed_relative_paths: tuple[str, ...]):
        WriteFileTool.__init__(self, workspace=workspace_root, allowed_dir=workspace_root)
        _RestrictedRepairPathMixin.__init__(
            self,
            workspace_root=workspace_root,
            allowed_relative_paths=allowed_relative_paths,
        )

    async def execute(self, path: str, content: str, **kwargs):
        self._validate_repair_path(path)
        return await super().execute(path=path, content=content, **kwargs)


class _RestrictedRepairEditFileTool(_RestrictedRepairPathMixin, EditFileTool):
    def __init__(self, *, workspace_root: Path, allowed_relative_paths: tuple[str, ...]):
        EditFileTool.__init__(self, workspace=workspace_root, allowed_dir=workspace_root)
        _RestrictedRepairPathMixin.__init__(
            self,
            workspace_root=workspace_root,
            allowed_relative_paths=allowed_relative_paths,
        )

    async def execute(self, path: str, old_text: str, new_text: str, **kwargs):
        self._validate_repair_path(path)
        return await super().execute(path=path, old_text=old_text, new_text=new_text, **kwargs)


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _TOOL_RESULT_MAX_CHARS = 500
    _FEISHU_REPAIR_ALLOWED_FILES = ("USER.md", "WORKLOG.md", "memory/MEMORY.md")

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
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
    ):
        from nanobot.config.schema import ExecToolConfig
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.reasoning_effort = reasoning_effort
        self.brave_api_key = brave_api_key
        self.web_proxy = web_proxy
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
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._consolidating: set[str] = set()  # Session keys with consolidation in progress
        self._pending_consolidations: set[str] = set()  # Session keys queued to consolidate after reply
        self._consolidation_tasks: set[asyncio.Task] = set()  # Strong refs to in-flight tasks
        self._archive_tasks: set[asyncio.Task] = set()  # Strong refs to /new archival tasks
        self._feishu_repair_tasks: set[asyncio.Task] = set()  # Strong refs to DM audit/repair tasks
        self._consolidation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._feishu_repair_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._session_generations: dict[str, int] = {}
        self._memory_stores: dict[Path, MemoryStore] = {}
        self._background_memory_lock = asyncio.Lock()
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
            path_append=self.exec_config.path_append,
        ))
        self.tools.register(WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except BaseException as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        self._set_tool_context_for_registry(self.tools, channel, chat_id, message_id)

    @staticmethod
    def _set_tool_context_for_registry(
        registry: ToolRegistry,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
    ) -> None:
        """Update context for all tools in the supplied registry that need routing info."""
        for name in ("message", "spawn", "cron"):
            if tool := registry.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        tool_registry: ToolRegistry | None = None,
        purpose: str | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop. Returns (final_content, tools_used, messages)."""
        active_tools = tool_registry or self.tools
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []

        while iteration < self.max_iterations:
            iteration += 1

            response = await self.provider.chat_with_retry(
                messages=messages,
                tools=active_tools.get_definitions(),
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                reasoning_effort=self.reasoning_effort,
                purpose=purpose,
                progress_callback=on_progress,
            )

            if response.has_tool_calls:
                if on_progress:
                    thought = self._strip_think(response.content)
                    if thought:
                        await on_progress(thought)
                    await on_progress(self._tool_hint(response.tool_calls), tool_hint=True)

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
                    result = await active_tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                clean = self._strip_think(response.content)
                # Don't persist error responses to session history — they can
                # poison the context and cause permanent 400 loops (#1303).
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    break
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

        return final_content, tools_used, messages

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            if msg.content.strip().lower() == "/stop":
                await self._handle_stop(msg)
            else:
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(lambda t, k=msg.session_key: self._active_tasks.get(k, []) and self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None)

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
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
        """Process a message under the session lock."""
        should_schedule_background = False
        async with self._get_session_lock(msg.session_key):
            try:
                response = await self._process_message(msg)
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
                should_schedule_background = True
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))
        if should_schedule_background:
            self._schedule_pending_consolidation(msg.session_key)

    def _get_session_lock(self, session_key: str) -> asyncio.Lock:
        """Return a per-session lock so different sessions can run concurrently."""
        lock = self._session_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_key] = lock
        return lock

    def _get_memory_store(self, memory_root: Path) -> MemoryStore:
        """Reuse MemoryStore instances so raw-archive fallback can span retries."""
        root = memory_root.resolve()
        store = self._memory_stores.get(root)
        if store is None:
            store = MemoryStore(root)
            self._memory_stores[root] = store
        return store

    def _get_session_generation(self, session_key: str) -> int:
        return self._session_generations.get(session_key, 0)

    def _bump_session_generation(self, session_key: str) -> int:
        generation = self._get_session_generation(session_key) + 1
        self._session_generations[session_key] = generation
        return generation

    async def close_mcp(self) -> None:
        """Drain background work, then close MCP connections."""
        pending_tasks = list(self._consolidation_tasks | self._archive_tasks | self._feishu_repair_tasks)
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        metadata = dict(msg.metadata or {})
        overlay_context = OverlayContext.from_metadata(metadata)
        metadata = overlay_context.to_metadata(metadata)

        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            return await self._run_with_session(
                msg,
                session=session,
                channel=channel,
                chat_id=chat_id,
                metadata=metadata,
                overlay_context=overlay_context,
                on_progress=on_progress,
                persist_session=True,
                allow_session_commands=False,
                enable_pending_consolidation=False,
                empty_response_fallback="Background task completed.",
            )

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)
        return await self._run_with_session(
            msg,
            session=session,
            channel=msg.channel,
            chat_id=msg.chat_id,
            metadata=metadata,
            overlay_context=overlay_context,
            on_progress=on_progress,
            persist_session=True,
            allow_session_commands=True,
            enable_pending_consolidation=True,
        )

    async def _run_with_session(
        self,
        msg: InboundMessage,
        *,
        session: Session,
        channel: str,
        chat_id: str,
        metadata: dict,
        overlay_context: OverlayContext,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        persist_session: bool,
        allow_session_commands: bool,
        enable_pending_consolidation: bool,
        extra_system_messages: list[str] | None = None,
        empty_response_fallback: str = "I've completed processing but have no response to give.",
        tool_registry: ToolRegistry | None = None,
    ) -> OutboundMessage | None:
        """Run one turn against the supplied session, optionally persisting it."""
        active_tools = tool_registry or self.tools
        session.metadata = overlay_context.to_metadata(session.metadata)

        # Slash commands
        cmd = msg.content.strip().lower()
        if allow_session_commands and cmd == "/new":
            self._pending_consolidations.discard(session.key)
            archival_session = session
            expected_generation = self._get_session_generation(session.key)
            self._bump_session_generation(session.key)

            replacement = Session(key=session.key)
            replacement.metadata = dict(session.metadata)
            self.sessions.save(replacement)

            self._schedule_background_archive(archival_session, expected_generation)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started.")
        if allow_session_commands and cmd == "/help":
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="🐈 nanobot commands:\n/new — Start a new conversation\n/stop — Stop the current task\n/help — Show available commands")

        if enable_pending_consolidation:
            unconsolidated = len(session.messages) - session.last_consolidated
            if (
                unconsolidated >= self._consolidation_trigger_threshold(session.key)
                and session.key not in self._consolidating
                and session.key not in self._pending_consolidations
            ):
                self._pending_consolidations.add(session.key)

        self._set_tool_context_for_registry(active_tools, channel, chat_id, metadata.get("message_id"))
        if message_tool := active_tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        system_messages = list(extra_system_messages or [])
        if feishu_dm_progress_rule := self._feishu_dm_progress_rule(
            channel=channel,
            overlay_context=overlay_context,
            allow_session_commands=allow_session_commands,
        ):
            system_messages.append(feishu_dm_progress_rule)
        system_messages.extend(self._channel_system_messages(channel))
        history = session.get_history(max_messages=self.memory_window)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=channel,
            chat_id=chat_id,
            runtime_metadata=metadata,
            extra_context=metadata.get("extra_context"),
            extra_system_messages=system_messages or None,
            system_overlay_root=str(overlay_context.root_path) if overlay_context.root_path else None,
            system_overlay_bootstrap=overlay_context.system_overlay_bootstrap,
        )
        turn_start_index = len(initial_messages) - 1

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(metadata)
            meta["_progress"] = True
            meta["_is_tool_progress"] = tool_hint
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=channel, chat_id=chat_id, content=content, metadata=meta,
            ))

        final_content, _, all_msgs = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
            tool_registry=active_tools,
        )

        if final_content is None:
            final_content = empty_response_fallback

        self._save_turn(session, all_msgs, turn_start_index)
        if persist_session:
            self.sessions.save(session)

        sent_via_message_tool = bool(
            (mt := active_tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn
        )

        self._maybe_log_feishu_placeholder_reply(
            channel=channel,
            chat_id=chat_id,
            turn_id=str(metadata.get("turn_id") or ""),
            content=final_content,
        )
        self._schedule_feishu_dm_turn_repair(
            session_key=session.key,
            msg=msg,
            metadata=metadata,
            overlay_context=overlay_context,
            final_content=final_content,
            turn_messages=all_msgs[turn_start_index:],
        )

        if sent_via_message_tool:
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", channel, msg.sender_id, preview)
        return OutboundMessage(
            channel=channel, chat_id=chat_id, content=final_content,
            metadata=metadata,
        )

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool" and isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                entry["content"] = content[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            elif role == "user":
                if ContextBuilder._SESSION_USER_CONTENT_KEY in entry:
                    entry["content"] = entry.pop(ContextBuilder._SESSION_USER_CONTENT_KEY)
                    content = entry["content"]
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # Strip the runtime-context prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if c.get("type") == "text" and isinstance(c.get("text"), str) and c["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                            continue  # Strip runtime context from multimodal messages
                        if (c.get("type") == "image_url"
                                and c.get("image_url", {}).get("url", "").startswith("data:image/")):
                            filtered.append({"type": "text", "text": "[image]"})
                        else:
                            filtered.append(c)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    async def _consolidate_memory(self, session, archive_all: bool = False) -> bool:
        """Delegate to MemoryStore.consolidate(). Returns True on success."""
        overlay_context = OverlayContext.from_metadata(session.metadata)
        memory_root = overlay_context.root_path or self.workspace
        return await self._get_memory_store(memory_root).consolidate(
            session, self.provider, self.model,
            archive_all=archive_all, memory_window=self.memory_window,
            temperature=self.temperature, max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
            purpose="memory_consolidation",
        )

    async def _archive_session_tail(self, session: Session) -> bool:
        """Archive the unconsolidated tail of a pre-reset session."""
        start = max(0, min(session.last_consolidated, len(session.messages)))
        messages = session.messages[start:]
        if not messages:
            return True

        overlay_context = OverlayContext.from_metadata(session.metadata)
        memory_root = overlay_context.root_path or self.workspace
        return await self._get_memory_store(memory_root).archive_messages(
            messages,
            self.provider,
            self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
            purpose="memory_consolidation",
        )

    def _consolidation_trigger_threshold(self, session_key: str | None = None) -> int:
        return max(20, self.memory_window // 2)

    def _schedule_pending_consolidation(self, session_key: str) -> None:
        if session_key not in self._pending_consolidations or session_key in self._consolidating:
            return

        self._pending_consolidations.discard(session_key)
        self._consolidating.add(session_key)
        session_lock = self._consolidation_locks.setdefault(session_key, asyncio.Lock())
        generation = self._get_session_generation(session_key)

        async def _run() -> None:
            try:
                async with self._background_memory_lock:
                    async with session_lock:
                        if self._get_session_generation(session_key) != generation:
                            return
                        session = self.sessions.get_or_create(session_key)
                        unconsolidated = len(session.messages) - session.last_consolidated
                        if unconsolidated < self._consolidation_trigger_threshold(session_key):
                            return
                        if await self._consolidate_memory(session):
                            if self._get_session_generation(session_key) == generation:
                                self.sessions.save(session)
            finally:
                self._consolidating.discard(session_key)
                task = asyncio.current_task()
                if task is not None:
                    self._consolidation_tasks.discard(task)

        task = asyncio.create_task(_run())
        self._consolidation_tasks.add(task)

    def _schedule_background_archive(self, session: Session, expected_generation: int) -> None:
        """Archive a reset session in the background after any in-flight consolidation."""
        if not session.messages:
            return

        session_lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())

        async def _run() -> None:
            try:
                async with self._background_memory_lock:
                    async with session_lock:
                        if self._get_session_generation(session.key) < expected_generation + 1:
                            return
                        await self._archive_session_tail(session)
            except Exception:
                logger.exception("Background /new archival failed for {}", session.key)
            finally:
                task = asyncio.current_task()
                if task is not None:
                    self._archive_tasks.discard(task)

        task = asyncio.create_task(_run())
        self._archive_tasks.add(task)

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        overlay_context: OverlayContext | None = None,
    ) -> str:
        """Process a message directly (for CLI or cron usage)."""
        await self._connect_mcp()
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            metadata=(overlay_context or OverlayContext()).to_metadata(),
        )
        async with self._get_session_lock(session_key):
            response = await self._process_message(msg, session_key=session_key, on_progress=on_progress)
        self._schedule_pending_consolidation(session_key)
        return response.content if response else ""

    async def process_heartbeat_direct(
        self,
        content: str,
        *,
        channel: str,
        chat_id: str,
        workspace_root: Path,
        overlay_context: OverlayContext | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> HeartbeatExecutionResult:
        """Process a heartbeat task in a fresh, non-persistent session."""
        await self._connect_mcp()
        ephemeral_key = f"heartbeat-run:{uuid.uuid4().hex}"
        active_overlay = overlay_context or OverlayContext(system_overlay_root=str(workspace_root))
        metadata = active_overlay.to_metadata()
        msg = InboundMessage(
            channel=channel,
            sender_id="heartbeat",
            chat_id=chat_id,
            content=content,
            metadata=metadata,
            session_key_override=ephemeral_key,
        )
        session = Session(key=ephemeral_key)
        extra_system_messages = [self._build_heartbeat_execution_message(workspace_root)]
        heartbeat_tools = self._build_heartbeat_tool_registry(workspace_root)

        try:
            async with self._get_session_lock(ephemeral_key):
                response = await self._run_with_session(
                    msg,
                    session=session,
                    channel=channel,
                    chat_id=chat_id,
                    metadata=metadata,
                    overlay_context=active_overlay,
                    on_progress=on_progress,
                    persist_session=False,
                    allow_session_commands=False,
                    enable_pending_consolidation=False,
                    extra_system_messages=extra_system_messages,
                    tool_registry=heartbeat_tools,
                )
        except Exception as exc:
            summary = self._build_heartbeat_state_summary("", session.messages, error=exc)
            raise HeartbeatExecutionError(summary, session.messages) from exc

        response_text = response.content if response else ""
        return HeartbeatExecutionResult(
            response_text=response_text,
            state_summary=self._build_heartbeat_state_summary(response_text, session.messages),
            transcript_messages=list(session.messages),
        )

    def _build_heartbeat_execution_message(self, workspace_root: Path) -> str:
        """Construct the heartbeat-only execution prompt block."""
        heartbeat_path = workspace_root / "HEARTBEAT.md"
        heartbeat_text = heartbeat_path.read_text(encoding="utf-8") if heartbeat_path.exists() else "(missing)"
        worklog_text = WorklogStore(workspace_root).read_full().strip() or "(missing)"
        return (
            "You are executing a heartbeat run. This is a fresh ephemeral run with no prior heartbeat transcript. "
            "Use HEARTBEAT.md for low-disturbance reminder rules and WORKLOG.md for current work state. "
            "You may read relevant files to check context and use the `message` tool to send a reminder when needed. "
            "Do not modify USER.md, SOUL.md, WORKLOG.md, memory/MEMORY.md, memory/HISTORY.md, or HEARTBEAT.md. "
            "Do not use shell, web, spawn, MCP, or cron capabilities during heartbeat execution. "
            "The HEARTBEAT_STATE managed block is written only by the framework, never by you.\n\n"
            f"## HEARTBEAT.md\n{heartbeat_text.strip()}"
            f"\n\n## WORKLOG.md\n{worklog_text}"
        )

    def _build_heartbeat_tool_registry(self, workspace_root: Path) -> ToolRegistry:
        """Build the restricted tool registry used only for heartbeat execution."""
        registry = ToolRegistry()
        allowed_dir = workspace_root if self.restrict_to_workspace else None
        registry.register(ReadFileTool(workspace=workspace_root, allowed_dir=allowed_dir))
        registry.register(MessageTool(send_callback=self.bus.publish_outbound))
        return registry

    def _build_feishu_repair_tool_registry(self, workspace_root: Path) -> ToolRegistry:
        """Build the restricted tool registry used only for Feishu DM repair."""
        registry = ToolRegistry()
        registry.register(
            _RestrictedRepairReadFileTool(
                workspace_root=workspace_root,
                allowed_relative_paths=self._FEISHU_REPAIR_ALLOWED_FILES,
            )
        )
        registry.register(
            _RestrictedRepairWriteFileTool(
                workspace_root=workspace_root,
                allowed_relative_paths=self._FEISHU_REPAIR_ALLOWED_FILES,
            )
        )
        registry.register(
            _RestrictedRepairEditFileTool(
                workspace_root=workspace_root,
                allowed_relative_paths=self._FEISHU_REPAIR_ALLOWED_FILES,
            )
        )
        return registry

    @classmethod
    def _should_schedule_feishu_dm_repair(
        cls,
        *,
        channel: str,
        metadata: dict,
        overlay_context: OverlayContext,
    ) -> bool:
        if channel != "feishu":
            return False
        if overlay_context.root_path is None:
            return False
        if str(metadata.get("chat_type") or "") != "p2p":
            return False
        if metadata.get("_skip_post_turn_audit"):
            return False
        return True

    def _schedule_feishu_dm_turn_repair(
        self,
        *,
        session_key: str,
        msg: InboundMessage,
        metadata: dict,
        overlay_context: OverlayContext,
        final_content: str,
        turn_messages: list[dict],
    ) -> None:
        if not self._should_schedule_feishu_dm_repair(
            channel=msg.channel,
            metadata=metadata,
            overlay_context=overlay_context,
        ):
            return

        overlay_root = overlay_context.root_path
        if overlay_root is None:
            return

        session_lock = self._feishu_repair_locks.setdefault(session_key, asyncio.Lock())
        captured_turn_messages = [copy.deepcopy(message) for message in turn_messages]
        bootstrap_active = overlay_context.system_overlay_bootstrap is not False and (overlay_root / "BOOTSTRAP.md").exists()

        async def _run() -> None:
            try:
                async with session_lock:
                    await self._run_feishu_dm_turn_repair(
                        overlay_root=overlay_root,
                        session_key=session_key,
                        chat_id=msg.chat_id,
                        turn_id=str(metadata.get("turn_id") or ""),
                        user_message=msg.content,
                        final_content=final_content,
                        turn_messages=captured_turn_messages,
                        bootstrap_active=bootstrap_active,
                    )
            except Exception:
                logger.exception("Feishu DM post-turn repair failed for {}", session_key)
            finally:
                task = asyncio.current_task()
                if task is not None:
                    self._feishu_repair_tasks.discard(task)

        task = asyncio.create_task(_run())
        self._feishu_repair_tasks.add(task)

    async def _run_feishu_dm_turn_repair(
        self,
        *,
        overlay_root: Path,
        session_key: str,
        chat_id: str,
        turn_id: str,
        user_message: str,
        final_content: str,
        turn_messages: list[dict],
        bootstrap_active: bool,
    ) -> None:
        repair_tools = self._build_feishu_repair_tool_registry(overlay_root)
        initial_messages = [
            {
                "role": "system",
                "content": self._build_feishu_repair_system_message(),
            },
            {
                "role": "user",
                "content": self._build_feishu_repair_user_message(
                    overlay_root=overlay_root,
                    session_key=session_key,
                    chat_id=chat_id,
                    turn_id=turn_id,
                    user_message=user_message,
                    final_content=final_content,
                    turn_messages=turn_messages,
                    bootstrap_active=bootstrap_active,
                ),
            },
        ]
        final_result, tools_used, _messages = await self._run_agent_loop(
            initial_messages,
            tool_registry=repair_tools,
            purpose="feishu_dm_post_turn_repair",
        )
        logger.bind(
            event="feishu_dm_turn_repair",
            session_key=session_key,
            chat_id=chat_id,
            turn_id=turn_id,
            tools_used=tools_used,
        ).info("Feishu DM post-turn repair completed with result={}", (final_result or "").strip() or "EMPTY")

    def _build_feishu_repair_system_message(self) -> str:
        return """You are running a Feishu DM post-turn audit and repair pass.

This is not a user-facing conversation. Do not chat, do not send messages, and do not explain yourself.
Your only job is to inspect the completed turn plus the current file state, then repair missing persistence if needed.

Allowed tools:
- read_file
- write_file
- edit_file

Allowed write targets only:
- USER.md
- WORKLOG.md
- memory/MEMORY.md

Hard rules:
- Never write any other file.
- Never send a message.
- Never touch BOOTSTRAP.md, HEARTBEAT.md, SOUL.md, or memory/HISTORY.md.
- Prefer the smallest correct file change.
- If the files already correctly capture the durable information from this turn, return exactly NO_OP and do not call any tool.
- If you make one or more file edits, return exactly REPAIRED when finished.

Persistence mapping:
- USER.md: nickname, preferred form of address, stable communication style, reply length preference, stable collaboration preference.
- memory/MEMORY.md: long-term work background, long-term project direction, durable facts that remain useful later.
- WORKLOG.md: current active task, current goal, next step, or deliverable for work in progress.

Decision guidance:
- Treat casual acknowledgements like “是的”, “好的”, “收到” as NO_OP unless they also add durable information.
- “我是 X / 叫我 X” usually belongs in USER.md.
- “结论先行 / 少铺垫 / 简洁一点 / 详细点” usually belongs in USER.md.
- “长期在做 X” usually belongs in memory/MEMORY.md.
- “上一个节点是 X / 本轮要交付 Y / 下一步做 Z” usually belongs in WORKLOG.md.
- Do not invent facts. Only persist explicit information or very high-confidence reformulations.
- When updating WORKLOG.md, follow the file's exact three-field schema and do not add extra fields.
"""

    def _build_feishu_repair_user_message(
        self,
        *,
        overlay_root: Path,
        session_key: str,
        chat_id: str,
        turn_id: str,
        user_message: str,
        final_content: str,
        turn_messages: list[dict],
        bootstrap_active: bool,
    ) -> str:
        user_text = self._read_repair_file(overlay_root / "USER.md")
        worklog_text = self._read_repair_file(overlay_root / "WORKLOG.md")
        memory_text = self._read_repair_file(overlay_root / "memory" / "MEMORY.md")
        transcript = self._format_turn_transcript(turn_messages)
        bootstrap_status = "active" if bootstrap_active else "inactive"
        return f"""Audit this completed Feishu DM turn and repair missing persistence only if needed.

## Turn Metadata
- Session key: {session_key}
- Chat ID: {chat_id}
- Turn ID: {turn_id or "(missing)"}
- BOOTSTRAP.md: {bootstrap_status}

## Current User Message
{user_message.strip() or "(empty)"}

## Final Assistant Reply
{(final_content or "").strip() or "(empty)"}

## Tool Transcript
{transcript}

## Current USER.md
{user_text}

## Current WORKLOG.md
{worklog_text}

## Current memory/MEMORY.md
{memory_text}
"""

    @staticmethod
    def _read_repair_file(path: Path) -> str:
        if not path.exists():
            return "(missing)"
        content = path.read_text(encoding="utf-8").strip()
        return content or "(empty)"

    def _format_turn_transcript(self, turn_messages: list[dict]) -> str:
        lines: list[str] = []
        for message in turn_messages:
            role = str(message.get("role") or "")
            if role == "user":
                text = self._serialize_message_content(message.get("content"))
                if text:
                    lines.append(f"USER: {text}")
            elif role == "assistant":
                if message.get("tool_calls"):
                    for tool_call in message.get("tool_calls") or []:
                        function = tool_call.get("function") or {}
                        name = function.get("name") or "unknown_tool"
                        arguments = function.get("arguments")
                        lines.append(f"ASSISTANT_TOOL_CALL: {name}({arguments})")
                text = self._serialize_message_content(message.get("content"))
                if text:
                    lines.append(f"ASSISTANT: {text}")
            elif role == "tool":
                name = message.get("name") or "tool"
                text = self._serialize_message_content(message.get("content"))
                if text:
                    lines.append(f"TOOL_RESULT[{name}]: {text[: self._TOOL_RESULT_MAX_CHARS]}")
        return "\n".join(lines) if lines else "(no tools used)"

    @classmethod
    def _serialize_message_content(cls, content) -> str:
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            text = "\n".join(parts)
        else:
            return ""
        if text.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
            parts = text.split("\n\n", 1)
            text = parts[1] if len(parts) > 1 else ""
        return " ".join(text.split()).strip()

    def _maybe_log_feishu_placeholder_reply(
        self,
        *,
        channel: str,
        chat_id: str,
        turn_id: str,
        content: str,
    ) -> None:
        if channel != "feishu":
            return
        if not is_framework_delayed_text(content):
            return
        logger.bind(
            event="feishu_model_placeholder_reply",
            turn_id=turn_id,
            chat_id=chat_id,
        ).warning("Final Feishu reply contains placeholder wording: {}", content[:200])

    def _build_heartbeat_state_summary(
        self,
        response_text: str,
        transcript_messages: list[dict],
        *,
        error: Exception | None = None,
    ) -> str:
        """Create a concise state summary for heartbeat managed state."""
        if error is not None:
            source = f"Execution failed: {error}"
        else:
            source = response_text.strip()
            if not source:
                for message in reversed(transcript_messages):
                    if message.get("role") != "assistant":
                        continue
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        source = content.strip()
                        break
            if not source:
                source = "Heartbeat execution completed."

        normalized = " ".join(str(source).split())
        if len(normalized) <= 280:
            return normalized
        return normalized[:277].rstrip() + "..."

    @staticmethod
    def _channel_system_messages(channel: str) -> list[str]:
        """Return channel-scoped delivery hints without polluting other transports."""
        if channel != "feishu":
            return []
        return [
            (
                "Feishu delivery rule: for proactive reminders, summaries, or follow-up notifications sent "
                "with the `message` tool on Feishu, prefer the structured `feishu_card` templates "
                "`notification` or `confirm`. Use `notification` for one-way updates and `confirm` only "
                "when you need the user to answer in chat. Do not use `message` or `feishu_card` for the "
                "assistant's normal reply in the current conversation turn; normal Feishu replies stay on "
                "the standard text/post path."
            )
        ]

    @staticmethod
    def _feishu_dm_progress_rule(*, channel: str, overlay_context: OverlayContext, allow_session_commands: bool) -> str | None:
        """Return a Feishu DM-only public action-note rule for tool-heavy turns."""
        if channel != "feishu" or overlay_context.root_path is None or not allow_session_commands:
            return None
        return (
            "飞书私聊里，只有在你准备进行多步工具操作时，先用一句可公开展示的短话说明你接下来要做什么，例如："
            "“先看现有实现，再定位问题。”\n"
            "问候、确认、直接回答和单个简单工具调用不要加这句话；不要说“让我想想”“正在处理”“稍后给你结果”，"
            "也不要暴露内部推理。"
        )
