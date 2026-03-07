"""用于组装智能体提示词的上下文构建器。"""

import base64
import mimetypes
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from nanobot.agent.prompt_context import PromptContext
from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader


class ContextBuilder:
    """构建智能体的上下文（系统提示词 + 消息历史记录）。"""

    COMMON_FILES = ["AGENTS.md", "TOOLS.md", "IDENTITY.md"]
    PRIVATE_CHAT_FILES = ["SOUL.md", "USER.md"]
    GROUP_CHAT_FILES = ["SOUL.md"]
    BOOTSTRAP_FILES = ["BOOTSTRAP.md"]
    HEARTBEAT_FILES = ["HEARTBEAT.md"]

    # region [初始化与提示词构建]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)
    
    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        runtime: PromptContext | None = None,
    ) -> str:
        """从身份设定、引导文件、记忆和技能中构建系统提示词。"""
        runtime = runtime or PromptContext()
        parts = [self._get_identity()]

        bootstrap = self._load_bootstrap_files(runtime)
        if bootstrap:
            parts.append(bootstrap)

        memory = self.memory.get_memory_context(runtime)
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")

        return "\n\n---\n\n".join(parts)
    
    def _get_identity(self) -> str:
        """获取核心身份设定部分。"""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"
        
        return f"""# nanobot 🐈

You are nanobot, a helpful AI assistant.

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
- Main-session long-term memory: {workspace_path}/MEMORY.md (write important facts here)
- Legacy memory compatibility path: {workspace_path}/memory/MEMORY.md
- History log: {workspace_path}/memory/HISTORY.md (grep-searchable). Each entry starts with [YYYY-MM-DD HH:MM].
- Feishu user profiles: {workspace_path}/memory/feishu/users/*.json
- Feishu group context: {workspace_path}/memory/feishu/chats/*.json
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

## nanobot Guidelines
- State intent before tool calls, but NEVER predict or claim results before receiving them.
- Before modifying a file, read it first. Do not assume files or directories exist.
- After writing or editing a file, re-read it if accuracy matters.
- If a tool call fails, analyze the error before retrying with a different approach.
- Ask for clarification when the request is ambiguous.

Reply directly with text for conversations. Only use the 'message' tool to send to a specific chat channel."""

    # endregion

    # region [运行时上下文与引导]

    @staticmethod
    def _build_runtime_context(channel: str | None, chat_id: str | None) -> str:
        """构建不受信任的运行时元数据块，用于注入到用户消息之前。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = time.strftime("%Z") or "UTC"
        lines = [f"Current Time: {now} ({tz})"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)
    
    def _resolve_workspace_files(self, runtime: PromptContext) -> list[str]:
        files = list(self.COMMON_FILES)
        if runtime.purpose == "bootstrap":
            files.extend(self.BOOTSTRAP_FILES)
        elif runtime.purpose == "heartbeat":
            files.extend(self.HEARTBEAT_FILES)
        elif runtime.is_feishu and runtime.is_group:
            files.extend(self.GROUP_CHAT_FILES)
        else:
            files.extend(self.PRIVATE_CHAT_FILES)
        return files

    def _load_bootstrap_files(self, runtime: PromptContext) -> str:
        """从工作区加载所有引导文件（bootstrap files）。"""
        parts = []

        for filename in self._resolve_workspace_files(runtime):
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")
        
        return "\n\n".join(parts) if parts else ""
    
    # endregion

    # region [消息列表构建]

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        runtime: PromptContext | None = None,
    ) -> list[dict[str, Any]]:
        """构建用于大语言模型调用的完整消息列表。"""
        runtime = runtime or PromptContext(channel=channel, chat_id=chat_id)
        messages = [
            {"role": "system", "content": self.build_system_prompt(skill_names, runtime=runtime)},
            *history,
            {"role": "user", "content": self._build_runtime_context(channel, chat_id)},
        ]
        if runtime.quoted_bot_summary:
            messages.append({
                "role": "user",
                "content": f"[Referenced Bot Message]\n{runtime.quoted_bot_summary}",
            })
        messages.append({"role": "user", "content": self._build_user_content(current_message, media)})
        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """构建包含可选 Base64 编码图片的用户消息内容。"""
        if not media:
            return text
        
        images = []
        for path in media:
            p = Path(path)
            mime, _ = mimetypes.guess_type(path)
            if not p.is_file() or not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(p.read_bytes()).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        
        if not images:
            return text
        return images + [{"type": "text", "text": text}]
    
    # endregion

    # region [消息追加与辅助方法]

    def add_tool_result(
        self, messages: list[dict[str, Any]],
        tool_call_id: str, tool_name: str, result: str,
    ) -> list[dict[str, Any]]:
        """将工具调用结果追加到消息列表中。"""
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result})
        return messages
    
    def add_assistant_message(
        self, messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """将助手的消息追加到消息列表中。"""
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if reasoning_content is not None:
            msg["reasoning_content"] = reasoning_content
        if thinking_blocks:
            msg["thinking_blocks"] = thinking_blocks
        messages.append(msg)
        return messages

    # endregion
