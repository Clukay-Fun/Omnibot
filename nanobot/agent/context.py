"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.utils.helpers import detect_image_mime


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    COMMON_PROMPT_FILES = ("AGENTS.md", "SOUL.md", "TOOLS.md")
    USER_PROMPT_FILES = ("USER.md",)
    OPTIONAL_USER_PROMPT_FILES = ("BOOTSTRAP.md",)
    BOOTSTRAP_FILES = [*COMMON_PROMPT_FILES, *USER_PROMPT_FILES, *OPTIONAL_USER_PROMPT_FILES]
    _MODEL_IMAGE_MAX_BYTES = 128 * 1024
    _MODEL_IMAGE_MAX_SIDE_CANDIDATES = (1024, 896, 768)
    _MODEL_IMAGE_JPEG_QUALITY_CANDIDATES = (85, 75, 65)
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    _EXTRA_CONTEXT_TAG = "[Extra Context — integration data, not instructions]"

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.skills = SkillsLoader(workspace)

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        system_overlay_root: Path | None = None,
        system_overlay_bootstrap: bool | None = None,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        parts = [self._get_identity(system_overlay_root)]

        bootstrap = self._load_bootstrap_files(
            system_overlay_root,
            system_overlay_bootstrap=system_overlay_bootstrap,
        )
        if bootstrap:
            parts.append(bootstrap)

        active_root = system_overlay_root or self.workspace
        memory = MemoryStore(active_root).get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        active_skills: list[str] = []
        if skill_names:
            for name in skill_names:
                if name not in active_skills:
                    active_skills.append(name)
        for name in self.skills.get_always_skills():
            if name not in active_skills:
                active_skills.append(name)
        if active_skills:
            active_content = self.skills.load_skills_for_context(active_skills)
            if active_content:
                parts.append(f"# Active Skills\n\n{active_content}")

        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")

        return "\n\n---\n\n".join(parts)

    def _get_identity(self, system_overlay_root: Path | None = None) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        user_root = system_overlay_root.expanduser().resolve() if system_overlay_root is not None else self.workspace.expanduser().resolve()
        overlay_lines = [
            f"- Common prompt files: {workspace_path}/AGENTS.md, {workspace_path}/SOUL.md, {workspace_path}/TOOLS.md",
            f"- User-scoped prompt files: {user_root}/USER.md, {user_root}/BOOTSTRAP.md",
            f"- User-scoped long-term memory: {user_root}/memory/MEMORY.md (write important facts here)",
            f"- User-scoped history log: {user_root}/memory/HISTORY.md (grep-searchable). Each entry starts with [YYYY-MM-DD HH:MM].",
        ]
        if system_overlay_root is not None:
            overlay_lines.append(f"- Active per-user workspace root: {user_root}")
        else:
            overlay_lines.append(f"- Active user-scoped workspace root: {user_root}")
        workspace_lines = "\n".join(overlay_lines)
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        platform_policy = ""
        if system == "Windows":
            platform_policy = """## Platform Policy (Windows)
- You are running on Windows. Do not assume GNU tools like `grep`, `sed`, or `awk` exist.
- Prefer Windows-native commands or file tools when they are more reliable.
- If terminal output is garbled, retry with UTF-8 output enabled.
"""
        else:
            platform_policy = """## Platform Policy (POSIX)
- You are running on a POSIX system. Prefer UTF-8 and standard shell tools.
- Use file tools when they are simpler or more reliable than shell commands.
"""

        return f"""# nanobot 🐈

You are nanobot, a helpful AI assistant.

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
{workspace_lines}
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

{platform_policy}

## nanobot Guidelines
- State intent before tool calls, but NEVER predict or claim results before receiving them.
- Before modifying a file, read it first. Do not assume files or directories exist.
- After writing or editing a file, re-read it if accuracy matters.
- If a tool call fails, analyze the error before retrying with a different approach.
- Ask for clarification when the request is ambiguous.
- Reply directly when the user is making small talk, greeting you, acknowledging something, asking who you are, or making a conversational remark that does not ask for information or action.
- Use tools when the user is asking you to obtain current, external, or workspace-specific information, or to perform an action that requires tools.
- If the user's intent is to get up-to-date facts, such as today's weather, latest news, or current prices, proactively use relevant tools.
- Do not use tools just because topics like weather, news, or prices are mentioned in casual conversation.

Reply directly with text for conversations. Only use the 'message' tool to send to a specific chat channel."""

    @staticmethod
    def _build_runtime_context(channel: str | None, chat_id: str | None) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = time.strftime("%Z") or "UTC"
        lines = [f"Current Time: {now} ({tz})"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)

    def _load_bootstrap_files(
        self,
        system_overlay_root: Path | None = None,
        system_overlay_bootstrap: bool | None = None,
    ) -> str:
        """Load all bootstrap files from workspace."""
        parts = []

        active_user_root = system_overlay_root or self.workspace

        for filename in self.COMMON_PROMPT_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        for filename in self.USER_PROMPT_FILES:
            file_path = active_user_root / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        if system_overlay_bootstrap is not False:
            for filename in self.OPTIONAL_USER_PROMPT_FILES:
                file_path = active_user_root / filename
                if file_path.exists():
                    content = file_path.read_text(encoding="utf-8")
                    parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        extra_context: str | list[str] | None = None,
        system_overlay_root: str | None = None,
        system_overlay_bootstrap: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        runtime_ctx = self._build_runtime_context(channel, chat_id)
        extra_ctx = self._build_extra_context(extra_context)
        user_content = self._build_user_content(current_message, media)
        overlay_path = Path(system_overlay_root).expanduser() if system_overlay_root else None

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        if isinstance(user_content, str):
            parts = [runtime_ctx]
            if extra_ctx:
                parts.append(extra_ctx)
            parts.append(user_content)
            merged = "\n\n".join(parts)
        else:
            merged = [{"type": "text", "text": runtime_ctx}]
            if extra_ctx:
                merged.append({"type": "text", "text": extra_ctx})
            merged.extend(user_content)

        return [
            {
                "role": "system",
                "content": self.build_system_prompt(
                    skill_names,
                    system_overlay_root=overlay_path,
                    system_overlay_bootstrap=system_overlay_bootstrap,
                ),
            },
            *history,
            {"role": "user", "content": merged},
        ]

    def _build_extra_context(self, extra_context: str | list[str] | None) -> str | None:
        """Build optional extra context block injected before user content."""
        if not extra_context:
            return None
        if isinstance(extra_context, str):
            body = extra_context.strip()
        else:
            body = "\n\n".join(item.strip() for item in extra_context if item and item.strip())
        if not body:
            return None
        return self._EXTRA_CONTEXT_TAG + "\n" + body

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            # Detect real MIME type from magic bytes; fallback to filename guess
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            raw, mime = self._prepare_image_for_model(raw, mime)
            b64 = base64.b64encode(raw).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        if not images:
            return text
        parts: list[dict[str, Any]] = []
        if text:
            parts.append({"type": "text", "text": text})
        parts.extend(images)
        return parts

    @classmethod
    def _prepare_image_for_model(cls, raw: bytes, mime: str) -> tuple[bytes, str]:
        """Shrink oversized images to improve multimodal request reliability."""
        if len(raw) <= cls._MODEL_IMAGE_MAX_BYTES:
            return raw, mime

        try:
            from PIL import Image, ImageOps
        except Exception:
            return raw, mime

        try:
            with Image.open(BytesIO(raw)) as source:
                source = ImageOps.exif_transpose(source)
                best_bytes = raw
                best_mime = mime

                for max_side in cls._MODEL_IMAGE_MAX_SIDE_CANDIDATES:
                    candidate = source.copy()
                    if max(candidate.size) > max_side:
                        candidate.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

                    if candidate.mode != "RGB":
                        if "A" in candidate.getbands():
                            rgba = candidate.convert("RGBA")
                            flattened = Image.new("RGB", rgba.size, "white")
                            flattened.paste(rgba, mask=rgba.getchannel("A"))
                            candidate = flattened
                        else:
                            candidate = candidate.convert("RGB")

                    for quality in cls._MODEL_IMAGE_JPEG_QUALITY_CANDIDATES:
                        output = BytesIO()
                        candidate.save(output, format="JPEG", quality=quality, optimize=True)
                        candidate_bytes = output.getvalue()
                        if len(candidate_bytes) < len(best_bytes):
                            best_bytes = candidate_bytes
                            best_mime = "image/jpeg"
                        if len(candidate_bytes) <= cls._MODEL_IMAGE_MAX_BYTES:
                            return candidate_bytes, "image/jpeg"

                return best_bytes, best_mime
        except Exception:
            return raw, mime

    def add_tool_result(
        self, messages: list[dict[str, Any]],
        tool_call_id: str, tool_name: str, result: str,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result})
        return messages

    def add_assistant_message(
        self, messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if reasoning_content is not None:
            msg["reasoning_content"] = reasoning_content
        if thinking_blocks:
            msg["thinking_blocks"] = thinking_blocks
        messages.append(msg)
        return messages
