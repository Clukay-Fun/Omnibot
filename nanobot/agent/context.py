"""用于组装智能体提示词的上下文构建器。"""

import base64
import json
import mimetypes
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from nanobot.agent.memory import MemoryStore
from nanobot.agent.prompt_context import PromptContext
from nanobot.agent.skill_runtime.registry import SkillSpecRegistry
from nanobot.agent.skills import SkillsLoader


class ContextBuilder:
    """构建智能体的上下文（系统提示词 + 消息历史记录）。"""

    SHARED_COMMON_FILES = ["AGENTS.md", "TOOLS.md"]
    PERSONA_COMMON_FILES = ["IDENTITY.md"]
    PRIVATE_CHAT_FILES = ["SOUL.md", "USER.md"]
    GROUP_CHAT_FILES = ["SOUL.md"]
    BOOTSTRAP_FILES = ["BOOTSTRAP.md"]
    HEARTBEAT_FILES = ["HEARTBEAT.md"]
    _LLM_TABLE_METADATA_LIMIT = 8
    _LLM_FIELD_METADATA_LIMIT = 12
    _SKILLSPEC_BLUEPRINT_LIMIT = 16
    _SKILLSPEC_DESCRIPTION_LIMIT = 120

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

        bootstrap_mode = self._build_bootstrap_mode_instructions(runtime)
        if bootstrap_mode:
            parts.append(bootstrap_mode)

        memory = self.memory.get_memory_context(runtime)
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        business_capabilities = self._build_business_capabilities_context(runtime)
        if business_capabilities:
            parts.append(business_capabilities)

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
- Private Feishu persona root: {workspace_path}/memory/feishu/users/<open_id>/{{BOOTSTRAP,SOUL,USER,IDENTITY,MEMORY}}.md
- Feishu user memory: {workspace_path}/memory/feishu/users/<open_id>/MEMORY.md
- Feishu group memory: {workspace_path}/memory/feishu/chats/<chat_id>/MEMORY.md
- Feishu thread memory: {workspace_path}/memory/feishu/threads/<chat_id>__<thread_id>/MEMORY.md
- Feishu user profiles (compat): {workspace_path}/memory/feishu/users/*.json
- Feishu group context (compat): {workspace_path}/memory/feishu/chats/*.json
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
    def _build_runtime_context(
        channel: str | None,
        chat_id: str | None,
        runtime: PromptContext | None = None,
    ) -> str:
        """构建不受信任的运行时元数据块，用于注入到用户消息之前。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = time.strftime("%Z") or "UTC"
        lines = [f"Current Time: {now} ({tz})"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if runtime is not None:
            workflow_mode = str(runtime.metadata.get("workflow_mode") or "").strip()
            if workflow_mode:
                lines.append(f"Workflow Mode: {workflow_mode}")
            thread_id = str(runtime.metadata.get("thread_id") or runtime.metadata.get("root_id") or "").strip()
            if thread_id:
                lines.append(f"Thread ID: {thread_id}")
            selected_table = runtime.recent_selected_table
            if selected_table:
                table_name = str(selected_table.get("table_name") or selected_table.get("name") or "").strip()
                table_id = str(selected_table.get("table_id") or "").strip()
                if table_name or table_id:
                    label = table_name or table_id
                    if table_name and table_id:
                        label = f"{table_name} ({table_id})"
                    lines.append(f"Recent Selected Table: {label}")
            directory_hits = runtime.recent_directory_hits[:3]
            if directory_hits:
                names = [str(item.get("display_name") or item.get("open_id") or "").strip() for item in directory_hits]
                names = [name for name in names if name]
                if names:
                    lines.append(f"Recent Directory Hits: {', '.join(names)}")
            for label, objects in (
                ("Recent Cases", runtime.recent_case_objects[:3]),
                ("Recent Contracts", runtime.recent_contract_objects[:3]),
                ("Recent Weekly Plans", runtime.recent_weekly_plan_objects[:3]),
            ):
                summaries = [str(item.get("display_label") or item.get("record_id") or "").strip() for item in objects]
                summaries = [item for item in summaries if item]
                if summaries:
                    lines.append(f"{label}: {'; '.join(summaries)}")
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)

    def _resolve_workspace_files(self, runtime: PromptContext) -> list[Path]:
        files = [self.workspace / filename for filename in self.SHARED_COMMON_FILES if (self.workspace / filename).exists()]

        if runtime.is_feishu and runtime.is_private:
            open_id = runtime.sender_id or runtime.chat_id
            if open_id:
                self.memory.ensure_feishu_user_persona_files(open_id)

        for filename in self.PERSONA_COMMON_FILES:
            path = self.memory.resolve_persona_markdown_path(filename, runtime)
            if path is not None and path.exists():
                files.append(path)

        if runtime.purpose == "bootstrap":
            file_names = list(self.BOOTSTRAP_FILES)
            if runtime.is_feishu and runtime.is_group:
                file_names.extend(self.GROUP_CHAT_FILES)
            else:
                file_names.extend(self.PRIVATE_CHAT_FILES)
        elif runtime.purpose == "heartbeat":
            file_names = self.HEARTBEAT_FILES
        elif runtime.is_feishu and runtime.is_group:
            file_names = self.GROUP_CHAT_FILES
        else:
            file_names = self.PRIVATE_CHAT_FILES

        for filename in file_names:
            if runtime.is_feishu and runtime.is_private and filename in {"BOOTSTRAP.md", "SOUL.md", "USER.md"}:
                path = self.memory.resolve_persona_markdown_path(filename, runtime)
            else:
                path = self.workspace / filename
            if path is not None and path.exists():
                files.append(path)
        return files

    def _load_bootstrap_files(self, runtime: PromptContext) -> str:
        """从工作区加载所有引导文件（bootstrap files）。"""
        parts = []

        for file_path in self._resolve_workspace_files(runtime):
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {file_path.name}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _build_bootstrap_mode_instructions(runtime: PromptContext) -> str:
        if runtime.purpose != "bootstrap":
            return ""

        lines = [
            "# Bootstrap Mode",
            "- Treat `BOOTSTRAP.md`, `SOUL.md`, `USER.md`, `IDENTITY.md`, and `MEMORY.md` as the source of truth.",
            "- Generate a natural conversational reply from those files instead of mechanically summarizing them.",
            "- Keep the reply usable as a real conversation turn, not a separate onboarding notice unless the files clearly call for that format.",
            "- Do not skip the bootstrap conversation just because the user's message is short, generic, or only a greeting.",
            "- Do not jump straight to a generic help offer like 'How can I help?' before bootstrap is addressed.",
        ]

        if runtime.metadata.get("_bootstrap_proactive"):
            lines.append(
                "- The user has just opened this private chat and has not sent a real message yet. Start the conversation proactively according to the current bootstrap files."
            )
        elif runtime.metadata.get("_bootstrap_reentry"):
            lines.append(
                "- The user explicitly asked to revisit setup. Re-open the conversation naturally based on the current bootstrap files."
            )
        else:
            lines.append(
                "- The user already sent a real first message. Reply to that actual message directly, but bootstrap still comes first and must be woven into the normal answer."
            )

        return "\n".join(lines)

    def _build_business_capabilities_context(self, runtime: PromptContext) -> str:
        if not runtime.is_feishu:
            return ""

        registry = SkillSpecRegistry(workspace_root=self.workspace / "skillspec")
        registry.load()
        blueprints = sorted(registry.blueprints.values(), key=lambda item: item.id)
        if not blueprints:
            return ""

        lines = [
            "# Business Capabilities",
            "Use these Feishu skillspec blueprints as concise business capability references. They help map user intent to tools and tables, but they do not change routing.",
        ]

        for blueprint in blueprints[: self._SKILLSPEC_BLUEPRINT_LIMIT]:
            lines.append(self._format_blueprint_for_prompt(blueprint))

        omitted = len(blueprints) - self._SKILLSPEC_BLUEPRINT_LIMIT
        if omitted > 0:
            lines.append(f"- additional_capabilities_omitted={omitted}")

        return "\n".join(lines)

    @classmethod
    def _format_blueprint_for_prompt(cls, blueprint: Any) -> str:
        title = cls._normalize_prompt_text(getattr(blueprint, "title", None))
        description = cls._normalize_prompt_text(getattr(blueprint, "description", None), limit=cls._SKILLSPEC_DESCRIPTION_LIMIT)
        action_kind = cls._normalize_prompt_text(getattr(blueprint, "action_kind", None)) or "unknown"
        primary_tool = cls._normalize_prompt_text(getattr(blueprint, "primary_tool", None)) or "unknown"

        table_aliases: list[str] = []
        for table in getattr(blueprint, "tables", []) or []:
            alias = cls._normalize_prompt_text(getattr(table, "alias", None))
            table_id = cls._normalize_prompt_text(getattr(table, "table_id", None))
            if alias:
                label = alias
            elif table_id:
                label = table_id
            else:
                continue
            if label not in table_aliases:
                table_aliases.append(label)
        tables = ", ".join(table_aliases) if table_aliases else "none"

        details = [
            f"id={blueprint.id}",
            f"title={title or blueprint.id}",
            f"kind={action_kind}",
            f"tool={primary_tool}",
            f"tables={tables}",
        ]
        if description:
            details.append(f"desc={description}")
        return "- " + "; ".join(details)

    @staticmethod
    def _normalize_prompt_text(value: Any, limit: int | None = None) -> str:
        if value is None:
            return ""
        text = " ".join(str(value).split())
        if limit is not None and len(text) > limit:
            return text[: max(0, limit - 3)].rstrip() + "..."
        return text

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
            {"role": "user", "content": self._build_runtime_context(channel, chat_id, runtime)},
        ]
        referenced_summary = str(runtime.referenced_message.get("summary") or runtime.quoted_bot_summary or "").strip()
        if referenced_summary:
            messages.append({
                "role": "user",
                "content": f"[Referenced Bot Message]\n{referenced_summary}",
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
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": tool_name,
                "content": self._compact_tool_result_for_llm(tool_name, result),
            }
        )
        return messages

    @classmethod
    def _compact_tool_result_for_llm(cls, tool_name: str, result: str) -> str:
        if tool_name not in {"bitable_list_tables", "bitable_list_fields"}:
            return result
        try:
            payload = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return result
        if not isinstance(payload, dict):
            return result

        if tool_name == "bitable_list_tables":
            tables = [item for item in payload.get("tables", []) if isinstance(item, dict)]
            if len(tables) <= cls._LLM_TABLE_METADATA_LIMIT:
                return result
            compact_payload = dict(payload)
            compact_payload["tables"] = tables[: cls._LLM_TABLE_METADATA_LIMIT]
            compact_payload["truncated_for_llm"] = True
            compact_payload["llm_table_limit"] = cls._LLM_TABLE_METADATA_LIMIT
            return json.dumps(compact_payload, ensure_ascii=False)

        fields = [item for item in payload.get("fields", []) if isinstance(item, dict)]
        if len(fields) <= cls._LLM_FIELD_METADATA_LIMIT:
            return result
        compact_payload = dict(payload)
        compact_payload["fields"] = fields[: cls._LLM_FIELD_METADATA_LIMIT]
        compact_payload["truncated_for_llm"] = True
        compact_payload["llm_field_limit"] = cls._LLM_FIELD_METADATA_LIMIT
        return json.dumps(compact_payload, ensure_ascii=False)

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
