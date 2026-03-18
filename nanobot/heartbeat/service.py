"""Heartbeat service - periodic agent wake-up to check for tasks."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from loguru import logger

from nanobot.agent.overlay import OverlayContext
from nanobot.heartbeat.types import HeartbeatExecutionError, HeartbeatExecutionResult

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
                    "summary": {
                        "type": "string",
                        "description": "Concise summary for the managed heartbeat state block.",
                    },
                },
                "required": ["action", "summary"],
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
    ``on_execute`` callback runs the task through a fresh ephemeral agent loop
    and returns a structured result for notification, state updates, and audit.
    """

    _LOG_RETENTION = 30
    _MAX_SUMMARY_CHARS = 280
    _STATE_BEGIN = "<!-- HEARTBEAT_STATE:BEGIN -->"
    _STATE_END = "<!-- HEARTBEAT_STATE:END -->"
    _STATE_HEADER = "## Last Heartbeat Run"

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        on_execute: Callable[[HeartbeatTarget, str], Coroutine[Any, Any, HeartbeatExecutionResult]] | None = None,
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

        return "\n\n".join(sections)

    @classmethod
    def _normalize_summary(cls, summary: str) -> str:
        text = " ".join((summary or "").split())
        if not text:
            text = "Heartbeat run completed."
        if len(text) <= cls._MAX_SUMMARY_CHARS:
            return text
        return text[: cls._MAX_SUMMARY_CHARS - 3].rstrip() + "..."

    @classmethod
    def _default_summary(cls, action: str, tasks: str = "") -> str:
        if action == "run" and tasks.strip():
            return cls._normalize_summary(tasks)
        return "No active heartbeat follow-up is needed."

    async def _decide(self, content: str) -> tuple[str, str, str]:
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
                {
                    "role": "user",
                    "content": (
                        "Review the following heartbeat context and decide whether there are active tasks. "
                        "Always provide a concise summary suitable for a managed status block.\n\n"
                        f"{content}"
                    ),
                },
            ],
            tools=_HEARTBEAT_TOOL,
            model=self.model,
        )

        if not response.has_tool_calls:
            return "skip", "", self._default_summary("skip")

        args = response.tool_calls[0].arguments
        if isinstance(args, str):
            args = json.loads(args)
        action = str((args or {}).get("action") or "skip")
        tasks = str((args or {}).get("tasks") or "")
        summary = str((args or {}).get("summary") or self._default_summary(action, tasks))
        return action, tasks, self._normalize_summary(summary)

    @classmethod
    def _render_state_block(cls, decision: str, summary: str, now: datetime | None = None) -> str:
        ts = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
        return (
            f"{cls._STATE_BEGIN}\n"
            f"{cls._STATE_HEADER}\n"
            f"- At: {ts}\n"
            f"- Decision: {decision}\n"
            f"- Summary: {cls._normalize_summary(summary)}\n"
            f"{cls._STATE_END}"
        )

    @classmethod
    def _strip_state_block(cls, text: str) -> str:
        pattern = re.compile(
            rf"\n?{re.escape(cls._STATE_BEGIN)}[\s\S]*?{re.escape(cls._STATE_END)}\n?",
            re.MULTILINE,
        )
        cleaned = pattern.sub("\n", text)

        lines = []
        for line in cleaned.splitlines():
            stripped = line.strip()
            if stripped in {cls._STATE_BEGIN, cls._STATE_END}:
                continue
            lines.append(line)
        cleaned = "\n".join(lines)

        legacy = re.compile(
            rf"(?ms)^\s*{re.escape(cls._STATE_HEADER)}\s*\n"
            r"(?:- At:.*\n)?"
            r"(?:- Decision:.*\n)?"
            r"(?:- Summary:.*(?:\n|$))?"
        )
        cleaned = legacy.sub("", cleaned)
        return cleaned.rstrip()

    def _upsert_state_block(self, target: HeartbeatTarget, decision: str, summary: str) -> None:
        heartbeat_path = target.heartbeat_file
        latest = self._read_text(heartbeat_path) or ""
        cleaned = self._strip_state_block(latest)
        block = self._render_state_block(decision, summary)
        if cleaned:
            new_text = cleaned + "\n\n" + block + "\n"
        else:
            new_text = block + "\n"
        heartbeat_path.write_text(new_text, encoding="utf-8")

    def _heartbeat_log_dir(self, target: HeartbeatTarget) -> Path:
        return target.workspace_root / "memory" / "heartbeat-logs"

    def _write_audit_log(
        self,
        target: HeartbeatTarget,
        *,
        decision: str,
        summary: str,
        transcript_messages: list[dict[str, Any]],
    ) -> None:
        log_dir = self._heartbeat_log_dir(target)
        log_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        ts = now.isoformat().replace("+00:00", "Z")
        filename = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}.jsonl"
        path = log_dir / filename

        metadata_line = {
            "_type": "metadata",
            "key": target.session_key,
            "created_at": ts,
            "updated_at": ts,
            "metadata": {
                "heartbeat": {
                    "channel": target.channel,
                    "chat_id": target.chat_id,
                    "decision": decision,
                    "summary": self._normalize_summary(summary),
                    "workspace_root": str(target.workspace_root),
                }
            },
            "last_consolidated": 0,
        }

        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
            for message in transcript_messages:
                f.write(json.dumps(message, ensure_ascii=False) + "\n")

        logs = sorted(log_dir.glob("*.jsonl"))
        for old in logs[:-self._LOG_RETENTION]:
            old.unlink(missing_ok=True)

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

        action, tasks, summary = await self._decide(content)
        if action != "run":
            self._upsert_state_block(target, "skip", summary)
            logger.info("Heartbeat: OK for {} (nothing to report)", target.workspace_root)
            return None

        logger.info("Heartbeat: tasks found for {}, executing...", target.workspace_root)
        if not self.on_execute:
            failure_summary = "Heartbeat execution callback is not configured."
            self._upsert_state_block(target, "failed", failure_summary)
            return None

        try:
            result = await self.on_execute(target, tasks)
        except HeartbeatExecutionError as exc:
            failure_summary = self._normalize_summary(exc.state_summary)
            self._upsert_state_block(target, "failed", failure_summary)
            self._write_audit_log(
                target,
                decision="failed",
                summary=failure_summary,
                transcript_messages=exc.transcript_messages,
            )
            logger.warning("Heartbeat: execution failed for {}: {}", target.workspace_root, failure_summary)
            return None
        except Exception as exc:
            failure_summary = self._normalize_summary(f"Execution failed: {exc}")
            self._upsert_state_block(target, "failed", failure_summary)
            self._write_audit_log(
                target,
                decision="failed",
                summary=failure_summary,
                transcript_messages=[],
            )
            logger.exception("Heartbeat execution failed for {}", target.workspace_root)
            return None

        state_summary = self._normalize_summary(result.state_summary)
        self._upsert_state_block(target, "run", state_summary)
        self._write_audit_log(
            target,
            decision="run",
            summary=state_summary,
            transcript_messages=result.transcript_messages,
        )
        if result.response_text and self.on_notify:
            logger.info("Heartbeat: completed for {}, delivering response", target.workspace_root)
            await self.on_notify(target, result.response_text)
        return result.response_text or None

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
