"""Heartbeat service - periodic agent wake-up to check for tasks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from loguru import logger

from nanobot.agent.overlay import OverlayContext

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider

_HEARTBEAT_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "heartbeat",
            "description": "Report heartbeat decision after reviewing tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["skip", "run"],
                        "description": "skip = nothing to do, run = has active tasks",
                    },
                    "tasks": {
                        "type": "string",
                        "description": "Natural-language summary of active tasks (required for run)",
                    },
                },
                "required": ["action"],
            },
        },
    }
]


@dataclass(slots=True)
class HeartbeatTarget:
    """A concrete heartbeat execution target."""

    workspace_root: Path
    channel: str
    chat_id: str
    session_key: str
    overlay_context: OverlayContext | None = None

    @property
    def heartbeat_file(self) -> Path:
        return self.workspace_root / "HEARTBEAT.md"


class HeartbeatService:
    """
    Periodic heartbeat service that wakes the agent to check for tasks.

    Phase 1 (decision): reads HEARTBEAT.md plus nearby user-scoped context and
    asks the LLM — via a virtual tool call — whether there are active tasks.

    Phase 2 (execution): only triggered when Phase 1 returns ``run``. The
    ``on_execute`` callback runs the task through the full agent loop and
    returns the result to deliver.
    """

    _HISTORY_TAIL_CHARS = 4000

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        on_execute: Callable[[HeartbeatTarget, str], Coroutine[Any, Any, str]] | None = None,
        on_notify: Callable[[HeartbeatTarget, str], Coroutine[Any, Any, None]] | None = None,
        interval_s: int = 30 * 60,
        enabled: bool = True,
        target_provider: Callable[[], list[HeartbeatTarget]] | None = None,
        fallback_target_provider: Callable[[], HeartbeatTarget] | None = None,
    ):
        self.workspace = workspace
        self.provider = provider
        self.model = model
        self.on_execute = on_execute
        self.on_notify = on_notify
        self.interval_s = interval_s
        self.enabled = enabled
        self.target_provider = target_provider
        self.fallback_target_provider = fallback_target_provider
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def heartbeat_file(self) -> Path:
        return self.workspace / "HEARTBEAT.md"

    def _fallback_target(self) -> HeartbeatTarget:
        if self.fallback_target_provider is not None:
            return self.fallback_target_provider()
        return HeartbeatTarget(
            workspace_root=self.workspace,
            channel="cli",
            chat_id="direct",
            session_key="heartbeat",
            overlay_context=None,
        )

    def _enumerate_targets(self) -> list[HeartbeatTarget]:
        explicit_targets = self.target_provider() if self.target_provider is not None else []
        logger.info("Heartbeat: scanning {} targets", len(explicit_targets))
        return explicit_targets or [self._fallback_target()]

    def _read_text(self, path: Path) -> str | None:
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return None

    def _build_decision_context(self, target: HeartbeatTarget) -> str | None:
        heartbeat_text = self._read_text(target.heartbeat_file)
        if not heartbeat_text or not heartbeat_text.strip():
            return None

        sections = [f"## HEARTBEAT.md\n{heartbeat_text.strip()}"]

        user_file = target.workspace_root / "USER.md"
        if user_text := self._read_text(user_file):
            sections.append(f"## USER.md\n{user_text.strip()}")

        memory_file = target.workspace_root / "memory" / "MEMORY.md"
        if memory_text := self._read_text(memory_file):
            sections.append(f"## memory/MEMORY.md\n{memory_text.strip()}")

        history_file = target.workspace_root / "memory" / "HISTORY.md"
        if history_text := self._read_text(history_file):
            history_text = history_text.strip()
            if history_text:
                sections.append(f"## memory/HISTORY.md (recent tail)\n{history_text[-self._HISTORY_TAIL_CHARS:]}")

        return "\n\n".join(sections)

    async def _decide(self, content: str) -> tuple[str, str]:
        """Phase 1: ask LLM to decide skip/run via virtual tool call."""
        response = await self.provider.chat_with_retry(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a heartbeat agent. Call the heartbeat tool to report your decision. "
                        "Run only when the supplied files show concrete, active follow-up work."
                    ),
                },
                {"role": "user", "content": f"Review the following heartbeat context and decide whether there are active tasks.\n\n{content}"},
            ],
            tools=_HEARTBEAT_TOOL,
            model=self.model,
        )

        if not response.has_tool_calls:
            return "skip", ""

        args = response.tool_calls[0].arguments
        return args.get("action", "skip"), args.get("tasks", "")

    async def start(self) -> None:
        """Start the heartbeat service."""
        if not self.enabled:
            logger.info("Heartbeat disabled")
            return
        if self._running:
            logger.warning("Heartbeat already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Heartbeat started (every {}s)", self.interval_s)

    def stop(self) -> None:
        """Stop the heartbeat service."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run_loop(self) -> None:
        """Main heartbeat loop."""
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Heartbeat error: {}", e)

    async def _run_target(self, target: HeartbeatTarget) -> str | None:
        content = self._build_decision_context(target)
        if not content:
            logger.debug("Heartbeat: HEARTBEAT.md missing or empty for {}", target.workspace_root)
            return None

        action, tasks = await self._decide(content)
        if action != "run":
            logger.info("Heartbeat: OK for {} (nothing to report)", target.workspace_root)
            return None

        logger.info("Heartbeat: tasks found for {}, executing...", target.workspace_root)
        if not self.on_execute:
            return None

        response = await self.on_execute(target, tasks)
        if response and self.on_notify:
            logger.info("Heartbeat: completed for {}, delivering response", target.workspace_root)
            await self.on_notify(target, response)
        return response

    async def _tick(self) -> None:
        """Execute a single heartbeat tick."""
        for target in self._enumerate_targets():
            try:
                await self._run_target(target)
            except Exception:
                logger.exception("Heartbeat execution failed for {}", target.workspace_root)

    async def trigger_now(self, target: HeartbeatTarget | None = None) -> str | None:
        """Manually trigger a heartbeat."""
        active_target = target or self._enumerate_targets()[0]
        return await self._run_target(active_target)
