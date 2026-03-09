"""描述:
主要功能:
    - 管理长期记忆与历史日志的读写和整合。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.agent.prompt_context import PromptContext
from nanobot.utils.helpers import ensure_dir, safe_filename

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session


#region 工具模式定义

_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "将记忆整合结果保存到持久化存储中。",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "概述关键事件/决定/主题的一段文字（2-5 句话）。"
                        "以 [YYYY-MM-DD HH:MM] 开头。包含有助于 grep 搜索的详细信息。",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "完整的、更新后的 Markdown 格式长期记忆。包括所有现有的事实以及新事实。如果没有新内容则原样返回。",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


#endregion

#region 记忆存储核心类


class MemoryStore:
    """用处，参数

    功能:
        - 提供记忆文件读写与会话整合能力。
    """

    def __init__(self, workspace: Path):
        """用处，参数

        功能:
            - 初始化记忆目录与文件路径。
        """
        self.memory_dir = ensure_dir(workspace / "memory")
        self.workspace = workspace
        self.memory_file = workspace / "MEMORY.md"
        self.legacy_memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"
        self.feishu_user_dir = ensure_dir(self.memory_dir / "feishu" / "users")
        self.feishu_chat_dir = ensure_dir(self.memory_dir / "feishu" / "chats")
        self.feishu_thread_dir = ensure_dir(self.memory_dir / "feishu" / "threads")

    _PRIVATE_PERSONA_FILES = ("BOOTSTRAP.md", "SOUL.md", "USER.md", "IDENTITY.md", "MEMORY.md")

    @staticmethod
    def _template_root() -> Path:
        return Path(__file__).resolve().parent.parent / "templates"

    def _shared_markdown_path(self, filename: str) -> Path | None:
        candidates: list[Path] = []
        if filename == "MEMORY.md":
            candidates.extend(
                [
                    self.memory_file,
                    self.legacy_memory_file,
                    self._template_root() / "memory" / "MEMORY.md",
                ]
            )
        else:
            candidates.extend([self.workspace / filename, self._template_root() / filename])

        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def feishu_user_persona_dir(self, open_id: str) -> Path:
        return ensure_dir(self.feishu_user_dir / safe_filename(open_id))

    def feishu_user_persona_path(self, open_id: str, filename: str) -> Path:
        if filename == "MEMORY.md":
            return self.feishu_user_memory_path(open_id)
        return self.feishu_user_persona_dir(open_id) / filename

    def ensure_feishu_user_persona_file(self, open_id: str, filename: str) -> Path:
        target = self.feishu_user_persona_path(open_id, filename)
        if target.exists():
            return target

        source = self._shared_markdown_path(filename)
        if source is None:
            return target

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        return target

    def ensure_feishu_user_persona_files(self, open_id: str) -> list[Path]:
        created: list[Path] = []
        for filename in self._PRIVATE_PERSONA_FILES:
            path = self.feishu_user_persona_path(open_id, filename)
            existed = path.exists()
            resolved = self.ensure_feishu_user_persona_file(open_id, filename)
            if not existed and resolved.exists():
                created.append(resolved)
        return created

    def resolve_persona_markdown_path(self, filename: str, runtime: PromptContext | None = None) -> Path | None:
        runtime = runtime or PromptContext()
        if runtime.is_feishu and runtime.is_private:
            open_id = runtime.sender_id or runtime.chat_id
            if open_id:
                path = self.ensure_feishu_user_persona_file(open_id, filename)
                if path.exists():
                    return path
        return self._shared_markdown_path(filename)

    def read_long_term(self, runtime: PromptContext | None = None) -> str:
        """用处，参数

        功能:
            - 读取长期记忆内容。
        """
        path = self.resolve_persona_markdown_path("MEMORY.md", runtime)
        return path.read_text(encoding="utf-8") if path and path.exists() else ""

    def write_long_term(self, content: str, runtime: PromptContext | None = None) -> None:
        """用处，参数

        功能:
            - 覆盖写入长期记忆内容。
        """
        runtime = runtime or PromptContext()
        if runtime.is_feishu and runtime.is_private:
            open_id = runtime.sender_id or runtime.chat_id
            if open_id:
                path = self.ensure_feishu_user_persona_file(open_id, "MEMORY.md")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                return
        self.memory_file.write_text(content, encoding="utf-8")

    def _read_json(self, path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _write_json(self, path: Path, payload: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def _read_markdown(path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def feishu_user_profile_path(self, open_id: str) -> Path:
        return self.feishu_user_dir / f"{safe_filename(open_id)}.json"

    def feishu_chat_context_path(self, chat_id: str) -> Path:
        return self.feishu_chat_dir / f"{safe_filename(chat_id)}.json"

    def feishu_user_memory_path(self, open_id: str) -> Path:
        return self.feishu_user_dir / safe_filename(open_id) / "MEMORY.md"

    def feishu_chat_memory_path(self, chat_id: str) -> Path:
        return self.feishu_chat_dir / safe_filename(chat_id) / "MEMORY.md"

    def feishu_thread_memory_path(self, chat_id: str, thread_id: str) -> Path:
        key = f"{safe_filename(chat_id)}__{safe_filename(thread_id)}"
        return self.feishu_thread_dir / key / "MEMORY.md"

    def read_feishu_user_memory(self, open_id: str) -> str:
        return self._read_markdown(self.feishu_user_memory_path(open_id))

    def read_feishu_chat_memory(self, chat_id: str) -> str:
        return self._read_markdown(self.feishu_chat_memory_path(chat_id))

    def read_feishu_thread_memory(self, chat_id: str, thread_id: str) -> str:
        return self._read_markdown(self.feishu_thread_memory_path(chat_id, thread_id))

    def read_feishu_user_profile(self, open_id: str) -> dict:
        return self._read_json(self.feishu_user_profile_path(open_id))

    def upsert_feishu_user_profile(self, open_id: str, patch: dict) -> dict:
        profile = self.read_feishu_user_profile(open_id)
        profile.update({k: v for k, v in patch.items() if v not in (None, "")})
        self._write_json(self.feishu_user_profile_path(open_id), profile)
        return profile

    def read_feishu_chat_context(self, chat_id: str) -> dict:
        return self._read_json(self.feishu_chat_context_path(chat_id))

    def upsert_feishu_chat_context(self, chat_id: str, patch: dict) -> dict:
        context = self.read_feishu_chat_context(chat_id)
        context.update({k: v for k, v in patch.items() if v not in (None, "")})
        self._write_json(self.feishu_chat_context_path(chat_id), context)
        return context

    @staticmethod
    def _render_mapping(title: str, payload: dict) -> str:
        lines = []
        for key, value in payload.items():
            if value in (None, "", [], {}):
                continue
            if isinstance(value, (list, tuple)):
                value = ", ".join(str(item) for item in value if str(item).strip())
            elif isinstance(value, dict):
                value = "; ".join(f"{k}={v}" for k, v in value.items() if v not in (None, "", [], {}))
            if value not in (None, ""):
                lines.append(f"- {key}: {value}")
        return f"## {title}\n" + "\n".join(lines) if lines else ""

    def append_history(self, entry: str) -> None:
        """用处，参数

        功能:
            - Batch 1 起禁用 HISTORY 落盘，保留接口兼容。
        """
        _ = entry

    def get_memory_context(self, runtime: PromptContext | None = None) -> str:
        """用处，参数

        功能:
            - 生成注入提示词的长期记忆片段。
        """
        runtime = runtime or PromptContext()
        parts: list[str] = []

        if runtime.purpose == "heartbeat":
            return ""

        if not runtime.is_feishu:
            long_term = self.read_long_term(runtime)
            if long_term:
                parts.append(f"## Long-term Memory\n{long_term}")

        if runtime.is_feishu and runtime.is_private:
            long_term = self.read_long_term(runtime)
            if long_term:
                parts.append(f"## Long-term Memory\n{long_term}")
            open_id = runtime.sender_id or runtime.chat_id
            if open_id:
                user_memory_path = self.feishu_user_memory_path(open_id)
                long_term_path = self.resolve_persona_markdown_path("MEMORY.md", runtime)
                user_memory = self.read_feishu_user_memory(open_id)
                if user_memory and user_memory_path != long_term_path:
                    parts.append(f"## Feishu User Memory\n{user_memory}")

        if runtime.is_feishu and runtime.is_group and runtime.chat_id:
            chat_memory = self.read_feishu_chat_memory(runtime.chat_id)
            if runtime.is_topic:
                if chat_memory:
                    parts.append(f"## Feishu Chat Memory\n{chat_memory}")
                thread_id = str(runtime.metadata.get("thread_id") or runtime.metadata.get("root_id") or "").strip()
                if thread_id:
                    thread_memory = self.read_feishu_thread_memory(runtime.chat_id, thread_id)
                    if thread_memory:
                        parts.append(f"## Feishu Thread Memory\n{thread_memory}")
            else:
                long_term = self.read_long_term(runtime)
                if long_term:
                    parts.append(f"## Long-term Memory\n{long_term}")
                if chat_memory:
                    parts.append(f"## Feishu Chat Memory\n{chat_memory}")

        if runtime.is_feishu and runtime.sender_id:
            profile = self.read_feishu_user_profile(runtime.sender_id)
            if profile:
                parts.append(self._render_mapping("Feishu User Profile", profile))

        if runtime.is_feishu and runtime.is_group and runtime.chat_id:
            chat_context = self.read_feishu_chat_context(runtime.chat_id)
            if chat_context:
                parts.append(self._render_mapping("Feishu Chat Context", chat_context))

        return "\n\n".join(part for part in parts if part)

    async def consolidate(
        self,
        session: Session,
        provider: LLMProvider,
        model: str,
        *,
        runtime: PromptContext | None = None,
        archive_all: bool = False,
        memory_window: int = 50,
    ) -> bool:
        """用处，参数

        功能:
            - 调用模型整合旧消息并更新记忆文件。
        """
        if archive_all:
            old_messages = session.messages
            keep_count = 0
            logger.info("Memory consolidation (archive_all): {} messages", len(session.messages))
        else:
            keep_count = memory_window // 2
            if len(session.messages) <= keep_count:
                return True
            if len(session.messages) - session.last_consolidated <= 0:
                return True
            old_messages = session.messages[session.last_consolidated:-keep_count]
            if not old_messages:
                return True
            logger.info("Memory consolidation: {} to consolidate, {} keep", len(old_messages), keep_count)

        lines = []
        for m in old_messages:
            if not m.get("content"):
                continue
            tools = f" [tools: {', '.join(m['tools_used'])}]" if m.get("tools_used") else ""
            lines.append(f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}{tools}: {m['content']}")

        current_memory = self.read_long_term(runtime)
        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{chr(10).join(lines)}"""

        try:
            response = await provider.chat(
                messages=[
                    {"role": "system", "content": "你是一个记忆整合智能体（memory consolidation agent）。请调用 save_memory 工具来处理和整合对话。"},
                    {"role": "user", "content": prompt},
                ],
                tools=_SAVE_MEMORY_TOOL,
                model=model,
            )

            if not response.has_tool_calls:
                logger.warning("Memory consolidation: LLM did not call save_memory, skipping")
                return False

            args = response.tool_calls[0].arguments
            # 某些模型供应商会将参数作为 JSON 字符串而非字典返回
            if isinstance(args, str):
                args = json.loads(args)
            if not isinstance(args, dict):
                logger.warning("Memory consolidation: unexpected arguments type {}", type(args).__name__)
                return False

            if entry := args.get("history_entry"):
                if not isinstance(entry, str):
                    entry = json.dumps(entry, ensure_ascii=False)
                self.append_history(entry)
            if update := args.get("memory_update"):
                if not isinstance(update, str):
                    update = json.dumps(update, ensure_ascii=False)
                if update != current_memory:
                    self.write_long_term(update, runtime)

            session.last_consolidated = 0 if archive_all else len(session.messages) - keep_count
            logger.info("Memory consolidation done: {} messages, last_consolidated={}", len(session.messages), session.last_consolidated)
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return False

#endregion
