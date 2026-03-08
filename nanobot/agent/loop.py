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
from typing import TYPE_CHECKING, Any, Awaitable, Callable, cast

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanobot.agent.memory_worker import MemoryScope, MemoryTurnTask, MemoryWriteWorker
from nanobot.agent.prompt_context import PromptContext
from nanobot.agent.runtime_texts import RuntimeTextCatalog
from nanobot.agent.skill_runtime import (
    EmbeddingSkillRouter,
    OutputGuard,
    ReminderRuntime,
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
from nanobot.storage.audit import AuditSink
from nanobot.storage.sqlite_store import SQLiteConnectionOptions, SQLiteStore
from nanobot.utils.helpers import get_state_path, migrate_legacy_path, sync_workspace_templates

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
    from nanobot.oauth.feishu import FeishuOAuthService


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
    _ONBOARDING_SETUP_FALLBACK_COMMANDS = ("/setup", "重新设置")
    _ONBOARDING_GUIDE_PROMPTED_KEY = "guide_prompted_at"
    _PENDING_TOPIC_METADATA_KEY = "pending_topic_titles"
    _BOOTSTRAP_INTERNAL_TRIGGER_PREFIX = "[Bootstrap Internal Trigger]"
    _SKILLSPEC_RENDER_MAX_TOKENS = 800
    _SKILLSPEC_RENDER_MAX_INPUT_CHARS = 2400
    _MEMORY_WRITE_TURN_THRESHOLD = 3
    _MEMORY_TOPIC_END_KEYWORDS = (
        "先这样",
        "结束",
        "结论",
        "收尾",
        "done",
    )

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
        llm_timeout_seconds: float = 90.0,
        stage_heartbeat_seconds: float = 15.0,
        skillspec_render_primary_timeout_seconds: float = 12.0,
        skillspec_render_retry_timeout_seconds: float = 6.0,
        feishu_oauth_service: "FeishuOAuthService | None" = None,
        state_db_path: Path | None = None,
        sqlite_options: SQLiteConnectionOptions | None = None,
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
        sync_workspace_templates(self.workspace, silent=True)
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
        self._llm_timeout_seconds = max(0.1, float(llm_timeout_seconds))
        self._stage_heartbeat_seconds = max(0.0, float(stage_heartbeat_seconds))
        self._skillspec_render_primary_timeout_seconds = max(
            0.1,
            float(skillspec_render_primary_timeout_seconds),
        )
        self._skillspec_render_retry_timeout_seconds = max(
            0.1,
            float(skillspec_render_retry_timeout_seconds),
        )

        self.context = ContextBuilder(workspace)
        self._memory_store = self.context.memory
        legacy_state_db_path = self.workspace / "memory" / "feishu" / "state.sqlite3"
        self._state_db_path = state_db_path or (get_state_path() / "feishu" / "state.sqlite3")
        migrate_legacy_path(legacy_state_db_path, self._state_db_path, related_suffixes=("-wal", "-shm", ".bak"))
        self._sqlite_options = sqlite_options
        self.sessions = session_manager or SessionManager(
            workspace,
            state_db_path=self._state_db_path,
            sqlite_options=self._sqlite_options,
        )
        self._sqlite = SQLiteStore(self._state_db_path, options=self._sqlite_options)
        audit_cleanup_interval_seconds = AuditSink.DEFAULT_CLEANUP_INTERVAL_SECONDS
        audit_event_retention_days = AuditSink.DEFAULT_EVENT_AUDIT_RETENTION_DAYS
        audit_message_index_retention_days = AuditSink.DEFAULT_FEISHU_MESSAGE_INDEX_RETENTION_DAYS
        self._memory_flush_threshold_private = self._MEMORY_WRITE_TURN_THRESHOLD
        self._memory_flush_threshold_group = self._MEMORY_WRITE_TURN_THRESHOLD
        self._memory_force_flush_on_topic_end = True
        self._memory_topic_end_keywords = tuple(self._MEMORY_TOPIC_END_KEYWORDS)
        if self.channels_config is not None:
            feishu_cfg = getattr(self.channels_config, "feishu", None)
            if feishu_cfg is not None:
                audit_cleanup_interval_seconds = float(
                    getattr(feishu_cfg, "audit_cleanup_interval_seconds", audit_cleanup_interval_seconds)
                )
                audit_event_retention_days = int(
                    getattr(feishu_cfg, "audit_event_retention_days", audit_event_retention_days)
                )
                audit_message_index_retention_days = int(
                    getattr(
                        feishu_cfg,
                        "audit_message_index_retention_days",
                        audit_message_index_retention_days,
                    )
                )
                self._memory_flush_threshold_private = max(
                    1,
                    int(
                        getattr(
                            feishu_cfg,
                            "memory_flush_threshold_private",
                            self._memory_flush_threshold_private,
                        )
                    ),
                )
                self._memory_flush_threshold_group = max(
                    1,
                    int(
                        getattr(
                            feishu_cfg,
                            "memory_flush_threshold_group",
                            self._memory_flush_threshold_group,
                        )
                    ),
                )
                self._memory_force_flush_on_topic_end = bool(
                    getattr(
                        feishu_cfg,
                        "memory_force_flush_on_topic_end",
                        self._memory_force_flush_on_topic_end,
                    )
                )
                configured_keywords = getattr(feishu_cfg, "memory_topic_end_keywords", None)
                if isinstance(configured_keywords, list):
                    cleaned_keywords = tuple(
                        item.strip()
                        for item in configured_keywords
                        if isinstance(item, str) and item.strip()
                    )
                    if cleaned_keywords:
                        self._memory_topic_end_keywords = cleaned_keywords
        self._audit_sink = AuditSink(
            self._sqlite,
            cleanup_interval_seconds=audit_cleanup_interval_seconds,
            event_audit_retention_days=audit_event_retention_days,
            feishu_message_index_retention_days=audit_message_index_retention_days,
        )
        memory_base_threshold = min(
            self._memory_flush_threshold_private,
            self._memory_flush_threshold_group,
        )
        self._memory_worker = MemoryWriteWorker(
            self.workspace,
            flush_threshold=memory_base_threshold,
        )
        self._workers_started = False
        self.tools = ToolRegistry()
        self._runtime_text = RuntimeTextCatalog.load(workspace)
        self._feishu_oauth_service = feishu_oauth_service
        self._answer_placeholder_text = self._runtime_text.prompt_text(
            "progress", "answer_placeholder", "🐈努力回答中..."
        )
        self._user_memory_store = UserMemoryStore(self.workspace)
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
            state_db_path=self._state_db_path,
            sqlite_options=self._sqlite_options,
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
        self._session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._template_router = TemplateRouter(runtime_text=self._runtime_text)
        self._template_renderer = TemplateRenderer(
            self.response_template_config.max_list_items,
            runtime_text=self._runtime_text,
        )
        self._stream_warmup_chars = 24
        self._stream_warmup_ms = 300
        if self.channels_config:
            feishu_cfg = getattr(self.channels_config, "feishu", None)
            if feishu_cfg is not None:
                self._stream_warmup_chars = max(1, int(getattr(feishu_cfg, "stream_answer_warmup_chars", 24)))
                self._stream_warmup_ms = max(0, int(getattr(feishu_cfg, "stream_answer_warmup_ms", 300)))
        self._plain_stream_warmup_chars = max(1, min(self._stream_warmup_chars, 8))
        self._plain_stream_warmup_ms = max(0, min(self._stream_warmup_ms, 120))
        self._register_default_tools()
        self._skillspec_registry: SkillSpecRegistry | None = None
        self._skillspec_runtime: SkillSpecExecutor | None = None
        self._init_skillspec_runtime()

    def _init_skillspec_runtime(self) -> None:
        if not self.skillspec_config.enabled:
            return

        from nanobot.agent.skill_runtime.table_registry import TableRegistry

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
            user_memory=self._user_memory_store,
            embedding_router=embedding_router,
            embedding_min_score=self.skillspec_config.embedding_min_score,
            route_log_enabled=self.skillspec_config.route_log_enabled,
            route_log_top_k=self.skillspec_config.route_log_top_k,
            reminder_runtime=ReminderRuntime(
                get_state_path() / "reminders.json",
                legacy_store_paths=[self.workspace / "reminders.json"],
            ),
            runtime_text=self._runtime_text,
            table_registry=TableRegistry(workspace=self.workspace),
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

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().isoformat()

    def _prompt_context_for_message(self, msg: InboundMessage, *, session_key: str | None = None) -> PromptContext:
        purpose = "chat"
        source_event_type = str((msg.metadata or {}).get("source_event_type") or "")
        if (msg.metadata or {}).get("_bootstrap") or source_event_type in {"p2p_chat_create", "im.chat.create"}:
            purpose = "bootstrap"
        return PromptContext(
            purpose=purpose,
            channel=msg.channel,
            chat_id=msg.chat_id,
            sender_id=msg.sender_id,
            session_key=session_key or msg.session_key,
            metadata=dict(msg.metadata or {}),
        )

    @staticmethod
    def _prompt_context_from_session(session: Session) -> PromptContext:
        metadata = dict(session.metadata or {})
        return PromptContext(
            purpose="chat",
            channel=str(metadata.get("channel") or "") or None,
            chat_id=str(metadata.get("chat_id") or "") or None,
            sender_id=str(metadata.get("sender_id") or "") or None,
            session_key=session.key,
            metadata=metadata,
        )

    @staticmethod
    def _remember_session_runtime_metadata(session: Session, msg: InboundMessage) -> None:
        metadata = dict(session.metadata or {})
        msg_metadata = dict(msg.metadata or {})
        metadata["channel"] = msg.channel
        metadata["chat_id"] = msg.chat_id
        metadata["sender_id"] = msg.sender_id
        metadata["chat_type"] = str(msg_metadata.get("chat_type") or "")

        thread_id = str(msg_metadata.get("thread_id") or msg_metadata.get("root_id") or "").strip()
        if thread_id:
            metadata["thread_id"] = thread_id
        else:
            metadata.pop("thread_id", None)

        session.metadata = metadata

    def _feishu_onboarding_config(self) -> Any | None:
        if not self.channels_config:
            return None
        feishu_cfg = getattr(self.channels_config, "feishu", None)
        if feishu_cfg is None:
            return None
        if not bool(getattr(feishu_cfg, "onboarding_enabled", False)):
            return None
        return feishu_cfg

    def _onboarding_enabled_for_message(self, msg: InboundMessage) -> bool:
        return msg.channel == "feishu" and self._feishu_onboarding_config() is not None

    def _onboarding_reentry_commands(self, feishu_cfg: Any) -> set[str]:
        configured = getattr(feishu_cfg, "onboarding_reentry_commands", None)
        commands: set[str] = set(self._ONBOARDING_SETUP_FALLBACK_COMMANDS)
        if isinstance(configured, list):
            commands.update({str(item).strip() for item in configured if str(item).strip()})
        return {cmd.lower() for cmd in commands}

    @staticmethod
    def _normalize_profile(profile: dict[str, Any]) -> dict[str, Any]:
        data = dict(profile)
        if not isinstance(data.get("identity"), dict):
            data["identity"] = {}
        if not isinstance(data.get("preferences"), dict):
            data["preferences"] = {}
        if not isinstance(data.get("dynamic"), dict):
            data["dynamic"] = {}
        if not isinstance(data.get("skillspec"), dict):
            data["skillspec"] = {}
        if not isinstance(data.get("onboarding"), dict):
            data["onboarding"] = {}
        return data

    @staticmethod
    def _extract_card_json_block(content: str, key: str) -> dict[str, Any]:
        target = f"{key}:"
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped.startswith(target):
                continue
            payload = stripped[len(target) :].strip()
            if not payload:
                return {}
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _normalize_form_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            for item in value:
                parsed = AgentLoop._normalize_form_value(item)
                if parsed:
                    return parsed
            return ""
        if isinstance(value, dict):
            for key in ("value", "id", "key", "name"):
                parsed = AgentLoop._normalize_form_value(value.get(key))
                if parsed:
                    return parsed
            text_part = value.get("text")
            if isinstance(text_part, dict):
                for key in ("content", "text", "name"):
                    parsed = AgentLoop._normalize_form_value(text_part.get(key))
                    if parsed:
                        return parsed
            return ""
        return ""

    def _extract_onboarding_action(self, msg: InboundMessage) -> tuple[str, dict[str, Any], dict[str, Any]]:
        metadata = msg.metadata or {}
        action_key = str(metadata.get("action_key") or "").strip()
        action_name = str(metadata.get("action_name") or "").strip()
        if not action_key:
            for line in msg.content.splitlines():
                stripped = line.strip()
                if stripped.startswith("action_key:"):
                    action_key = stripped.split(":", 1)[1].strip()
                    break
        if not action_name:
            for line in msg.content.splitlines():
                stripped = line.strip()
                if stripped.startswith("action_name:"):
                    action_name = stripped.split(":", 1)[1].strip()
                    break

        action_payload = self._extract_card_json_block(msg.content, "action")
        action_value = self._extract_card_json_block(msg.content, "action_value")
        if not action_key and isinstance(action_value, dict):
            action_key = self._normalize_form_value(
                action_value.get("action_key") or action_value.get("action") or action_value.get("name")
            )
        if not action_key and isinstance(action_payload, dict):
            action_key = self._normalize_form_value(
                action_payload.get("action_key") or action_payload.get("action") or action_payload.get("name")
            )
        if not action_name and isinstance(action_payload, dict):
            action_name = self._normalize_form_value(action_payload.get("name"))
        if not action_key and action_name:
            action_key = action_name

        form_value = self._extract_card_json_block(msg.content, "form_value")
        if not form_value and isinstance(action_payload, dict):
            payload_form = action_payload.get("form_value")
            if isinstance(payload_form, dict):
                form_value = payload_form
        if isinstance(form_value, dict) and len(form_value) == 1:
            nested = form_value.get("onboarding_form")
            if isinstance(nested, dict):
                form_value = nested

        return action_key, action_value, form_value

    @staticmethod
    def _guess_preferred_name(user_name: str, role: str) -> str:
        if not user_name:
            return ""
        if user_name.endswith("律师") and len(user_name) > 2:
            return f"{user_name[:-2]}律"
        if role in {"lawyer", "律师"} and not user_name.endswith("律"):
            return f"{user_name}律"
        return ""

    def _build_onboarding_single_card(self, feishu_cfg: Any, profile: dict[str, Any] | None = None) -> str:
        normalized_profile = self._normalize_profile(profile or {})
        identity = cast(dict[str, Any], normalized_profile["identity"])
        preferences = cast(dict[str, Any], normalized_profile["preferences"])
        skillspec = cast(dict[str, Any], normalized_profile["skillspec"])
        onboarding_tpl = self._runtime_text.template("onboarding_form")

        header_tpl = onboarding_tpl.get("header") if isinstance(onboarding_tpl.get("header"), dict) else {}
        labels_tpl = onboarding_tpl.get("labels") if isinstance(onboarding_tpl.get("labels"), dict) else {}
        placeholders_tpl = (
            onboarding_tpl.get("placeholders") if isinstance(onboarding_tpl.get("placeholders"), dict) else {}
        )
        sections_tpl = onboarding_tpl.get("sections") if isinstance(onboarding_tpl.get("sections"), dict) else {}
        buttons_tpl = onboarding_tpl.get("buttons") if isinstance(onboarding_tpl.get("buttons"), dict) else {}
        tone_options_tpl = onboarding_tpl.get("tone_options") if isinstance(onboarding_tpl.get("tone_options"), list) else []
        confirm_write_options_tpl = (
            onboarding_tpl.get("confirm_write_options")
            if isinstance(onboarding_tpl.get("confirm_write_options"), list)
            else []
        )
        query_scope_options_tpl = (
            onboarding_tpl.get("query_scope_options")
            if isinstance(onboarding_tpl.get("query_scope_options"), list)
            else []
        )

        user_name_default = self._normalize_form_value(identity.get("name"))
        role_for_guess = self._normalize_form_value(identity.get("role"))
        display_name_default = self._normalize_form_value(preferences.get("preferred_name"))
        if not display_name_default:
            display_name_default = self._guess_preferred_name(user_name_default, role_for_guess)

        tone_default = self._normalize_form_value(preferences.get("response_style"))
        if tone_default not in {"concise", "standard", "detailed"}:
            tone_default = ""

        query_scope_default = self._normalize_form_value(preferences.get("query_scope"))
        if query_scope_default not in {"self", "all"}:
            query_scope_default = ""

        confirm_pref = self._normalize_form_value(skillspec.get("confirm_preference"))
        if confirm_pref == "auto":
            confirm_write_default = "no"
        elif confirm_pref == "manual":
            confirm_write_default = "yes"
        else:
            confirm_write_default = ""

        card = {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": str(header_tpl.get("title") or "Welcome")},
                "subtitle": {
                    "tag": "plain_text",
                    "content": str(header_tpl.get("subtitle") or "Complete setup in one minute."),
                },
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": str(onboarding_tpl.get("intro_markdown") or ""),
                },
                {
                    "tag": "hr",
                },
                {
                    "tag": "form",
                    "name": "onboarding_form",
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": str(sections_tpl.get("identity") or "**Basic Info**"),
                        },
                        {
                            "tag": "input",
                            "name": "user_name",
                            "label": {"tag": "plain_text", "content": str(labels_tpl.get("name") or "Name")},
                            "placeholder": {
                                "tag": "plain_text",
                                "content": str(placeholders_tpl.get("name") or "Auto-filled from profile"),
                            },
                            "default_value": user_name_default,
                            "required": False,
                        },
                        {
                            "tag": "hr",
                        },
                        {
                            "tag": "markdown",
                            "content": str(sections_tpl.get("preferences") or "**Preferences**"),
                        },
                        {
                            "tag": "select_static",
                            "name": "tone",
                            "label": {"tag": "plain_text", "content": str(labels_tpl.get("tone") or "Tone")},
                            "placeholder": {"tag": "plain_text", "content": str(placeholders_tpl.get("select") or "Select")},
                            "initial_option": tone_default,
                            "options": [
                                {
                                    "text": {"tag": "plain_text", "content": str(item.get("text") or "")},
                                    "value": str(item.get("value") or ""),
                                }
                                for item in tone_options_tpl
                                if isinstance(item, dict)
                                and str(item.get("text") or "").strip()
                                and str(item.get("value") or "").strip()
                            ],
                        },
                        {
                            "tag": "column_set",
                            "flex_mode": "bisect",
                            "columns": [
                                {
                                    "tag": "column",
                                    "width": "weighted",
                                    "weight": 1,
                                    "elements": [
                                        {
                                            "tag": "select_static",
                                            "name": "confirm_write",
                                            "label": {
                                                "tag": "plain_text",
                                                "content": str(labels_tpl.get("confirm_write") or "Write behavior"),
                                            },
                                            "placeholder": {
                                                "tag": "plain_text",
                                                "content": str(placeholders_tpl.get("select") or "Select"),
                                            },
                                            "initial_option": confirm_write_default,
                                            "options": [
                                                {
                                                    "text": {"tag": "plain_text", "content": str(item.get("text") or "")},
                                                    "value": str(item.get("value") or ""),
                                                }
                                                for item in confirm_write_options_tpl
                                                if isinstance(item, dict)
                                                and str(item.get("text") or "").strip()
                                                and str(item.get("value") or "").strip()
                                            ],
                                        }
                                    ],
                                },
                                {
                                    "tag": "column",
                                    "width": "weighted",
                                    "weight": 1,
                                    "elements": [
                                        {
                                            "tag": "select_static",
                                            "name": "query_scope",
                                            "label": {
                                                "tag": "plain_text",
                                                "content": str(labels_tpl.get("query_scope") or "Default scope"),
                                            },
                                            "placeholder": {
                                                "tag": "plain_text",
                                                "content": str(placeholders_tpl.get("select") or "Select"),
                                            },
                                            "initial_option": query_scope_default,
                                            "options": [
                                                {
                                                    "text": {"tag": "plain_text", "content": str(item.get("text") or "")},
                                                    "value": str(item.get("value") or ""),
                                                }
                                                for item in query_scope_options_tpl
                                                if isinstance(item, dict)
                                                and str(item.get("text") or "").strip()
                                                and str(item.get("value") or "").strip()
                                            ],
                                        }
                                    ],
                                },
                            ],
                        },
                        {
                            "tag": "input",
                            "name": "display_name",
                            "label": {
                                "tag": "plain_text",
                                "content": str(labels_tpl.get("display_name") or "Preferred name"),
                            },
                            "placeholder": {
                                "tag": "plain_text",
                                "content": str(placeholders_tpl.get("display_name") or "Optional"),
                            },
                            "default_value": display_name_default,
                            "required": False,
                        },
                        {
                            "tag": "markdown",
                            "content": str(onboarding_tpl.get("footer_hint") or ""),
                        },
                        {
                            "tag": "column_set",
                            "flex_mode": "bisect",
                            "columns": [
                                {
                                    "tag": "column",
                                    "width": "weighted",
                                    "weight": 1,
                                    "elements": [
                                        {
                                            "tag": "button",
                                            "text": {
                                                "tag": "plain_text",
                                                "content": str(buttons_tpl.get("submit") or "Submit"),
                                            },
                                            "type": "primary",
                                            "action_type": "form_submit",
                                            "name": "submit_onboarding",
                                        }
                                    ],
                                },
                                {
                                    "tag": "column",
                                    "width": "weighted",
                                    "weight": 1,
                                    "elements": [
                                        {
                                            "tag": "button",
                                            "text": {
                                                "tag": "plain_text",
                                                "content": str(buttons_tpl.get("skip") or "Skip"),
                                            },
                                            "type": "default",
                                            "action_type": "form_submit",
                                            "name": "skip_onboarding",
                                        }
                                    ],
                                },
                            ],
                        },
                    ],
                },
            ],
        }
        return json.dumps(card, ensure_ascii=False)

    @staticmethod
    def _onboarding_defaults() -> dict[str, Any]:
        return {
            "identity": {
                "name": "",
                "role": "",
                "team": "",
            },
            "preferences": {
                "response_style": "standard",
                "preferred_name": "",
                "query_scope": "self",
            },
            "skillspec_confirm_preference": "manual",
        }

    def _resolve_profile_display_name(self, profile: dict[str, Any] | None = None) -> str:
        normalized_profile = self._normalize_profile(profile or {})
        identity = cast(dict[str, Any], normalized_profile["identity"])
        preferences = cast(dict[str, Any], normalized_profile["preferences"])

        preferred_name = self._normalize_form_value(preferences.get("preferred_name"))
        if preferred_name:
            return preferred_name

        user_name = self._normalize_form_value(identity.get("name"))
        role = self._normalize_form_value(identity.get("role"))
        guessed = self._guess_preferred_name(user_name, role)
        if guessed:
            return guessed
        if user_name:
            return user_name
        return "你"

    def _resolve_onboarding_name_example(self, profile: dict[str, Any] | None = None) -> str:
        preferred_name = self._resolve_profile_display_name(profile)
        return preferred_name if preferred_name != "你" else "小律"

    @staticmethod
    def _workspace_template_path(filename: str) -> Path:
        return Path(__file__).resolve().parent.parent / "templates" / filename

    def _read_workspace_or_template_file(self, filename: str, runtime: PromptContext | None = None) -> str:
        if runtime is not None and filename in {"BOOTSTRAP.md", "SOUL.md", "USER.md", "IDENTITY.md", "MEMORY.md"}:
            path = self._memory_store.resolve_persona_markdown_path(filename, runtime)
            if path is not None:
                try:
                    if path.exists():
                        return path.read_text(encoding="utf-8")
                except OSError:
                    pass

        for path in (self.workspace / filename, self._workspace_template_path(filename)):
            try:
                if path.exists():
                    return path.read_text(encoding="utf-8")
            except OSError:
                continue
        return ""

    def _build_bootstrap_identity_summary(self, runtime: PromptContext | None = None) -> str:
        bootstrap_text = self._read_workspace_or_template_file("BOOTSTRAP.md", runtime)
        topics = re.findall(r"\d+\.\s+\*\*(.*?)\*\*", bootstrap_text)
        normalized_topics = [topic.strip().lower() for topic in topics if topic.strip()]
        if any("name" in topic for topic in normalized_topics):
            return "名字 / 类型 / 语气 / emoji"
        return "名字 / 语气 / 行为偏好"

    def _build_bootstrap_action_summary(self, runtime: PromptContext | None = None) -> str:
        soul_text = self._read_workspace_or_template_file("SOUL.md", runtime)
        lower_soul = soul_text.lower()
        parts: list[str] = []
        if "concise" in lower_soul or "简洁" in soul_text:
            parts.append("默认简洁直接")
        if "accuracy" in lower_soul or "准确" in soul_text:
            parts.append("先保证准确")
        if "privacy" in lower_soul or "隐私" in soul_text:
            parts.append("重视隐私")
        if "ask before external actions" in lower_soul or "外部动作" in soul_text:
            parts.append("外部动作先确认")
        if not parts:
            parts.extend(["默认简洁直接", "重视准确与隐私", "外部动作先确认"])
        return "、".join(parts[:3])

    @staticmethod
    def _extract_markdown_list_items(text: str, pattern: str, *, limit: int | None = None) -> list[str]:
        items = [match.strip() for match in re.findall(pattern, text, flags=re.MULTILINE) if match.strip()]
        return items[:limit] if limit is not None else items

    @staticmethod
    def _extract_first_blockquote(text: str) -> str:
        quotes = [match.strip() for match in re.findall(r"^>\s*(.+)$", text, flags=re.MULTILINE) if match.strip()]
        return quotes[0] if quotes else ""

    @staticmethod
    def _extract_section_bullets(text: str, section_titles: tuple[str, ...], *, limit: int = 3) -> list[str]:
        targets = {title.lower() for title in section_titles}
        current_section = ""
        bullets: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("## "):
                current_section = line[3:].strip().lower()
                continue
            if current_section in targets and re.match(r"^[-*]\s+", line):
                bullets.append(re.sub(r"^[-*]\s+", "", line).strip())
                if len(bullets) >= limit:
                    return bullets
        if bullets:
            return bullets[:limit]
        fallback = [
            re.sub(r"^[-*]\s+", "", line.strip()).strip()
            for line in text.splitlines()
            if re.match(r"^\s*[-*]\s+", line)
        ]
        return [item for item in fallback if item][:limit]

    def _build_dynamic_onboarding_guide_lines(
        self,
        profile: dict[str, Any] | None = None,
        runtime: PromptContext | None = None,
    ) -> list[str]:
        preferred_name = self._resolve_onboarding_name_example(profile)
        bootstrap_text = self._read_workspace_or_template_file("BOOTSTRAP.md", runtime)
        soul_text = self._read_workspace_or_template_file("SOUL.md", runtime)

        bootstrap_items = self._extract_markdown_list_items(
            bootstrap_text,
            r"^\d+\.\s+(.+)$",
            limit=4,
        )
        bootstrap_quote = self._extract_first_blockquote(bootstrap_text)
        update_targets = self._extract_markdown_list_items(
            bootstrap_text,
            r"^[-*]\s+(`[^`]+`.+)$",
            limit=4,
        )
        soul_items = self._extract_section_bullets(
            soul_text,
            ("Response Defaults", "Boundaries", "Core Values"),
            limit=4,
        )

        lines = [
            "### 👋 BOOTSTRAP 确认",
            "我会先按当前 `BOOTSTRAP.md` / `SOUL.md` 的内容继续，不阻塞对话。",
            "",
        ]

        if bootstrap_items:
            lines.append("当前 `BOOTSTRAP.md` 里优先确认：")
            lines.extend(f"- {item}" for item in bootstrap_items)
            lines.append("")
        else:
            lines.extend([
                "默认先确认两类事：",
                f"- 人格：{self._build_bootstrap_identity_summary(runtime)}",
                f"- 行动方式：{self._build_bootstrap_action_summary(runtime)}",
                "",
            ])

        if bootstrap_quote:
            lines.append("当前建议开场原文：")
            lines.append(f"> {bootstrap_quote}")
            lines.append("")

        if update_targets:
            lines.append("确认后会更新：")
            lines.extend(f"- {item}" for item in update_targets)
            lines.append("")

        if soul_items:
            lines.append("当前 `SOUL.md` 强调：")
            lines.extend(f"- {item}" for item in soul_items)
            lines.append("")

        lines.extend(
            [
                f"直接回复你想改的点即可；例如名字改成“{preferred_name}”、调整语气、边界或写入确认方式。",
                "如果你不改，我就按默认继续。需要重看这条提示可以发 `/setup`。",
            ]
        )
        return lines

    def _build_onboarding_guide_message(
        self,
        profile: dict[str, Any] | None = None,
        runtime: PromptContext | None = None,
    ) -> str:
        if runtime is not None and runtime.is_feishu and runtime.is_private:
            open_id = runtime.sender_id or runtime.chat_id
            if open_id:
                self._memory_store.ensure_feishu_user_persona_files(open_id)
        preferred_name = self._resolve_onboarding_name_example(profile)
        bootstrap_identity = self._build_bootstrap_identity_summary(runtime)
        bootstrap_action = self._build_bootstrap_action_summary(runtime)
        if self._runtime_text.has_prompt_override("onboarding", "guide_lines"):
            lines = self._runtime_text.prompt_lines("onboarding", "guide_lines", [])
        else:
            lines = self._build_dynamic_onboarding_guide_lines(profile, runtime)
        rendered_lines = [
            line.replace("{preferred_name}", preferred_name)
            .replace("{bootstrap_identity}", bootstrap_identity)
            .replace("{bootstrap_action}", bootstrap_action)
            for line in lines
        ]
        return "\n".join(rendered_lines)

    def _build_onboarding_guide_outbound(
        self,
        msg: InboundMessage,
        *,
        stage: str,
        intro_text: str,
        profile: dict[str, Any] | None = None,
    ) -> OutboundMessage:
        metadata = dict(msg.metadata or {})
        metadata["onboarding"] = True
        metadata["onboarding_stage"] = stage
        metadata["_reply_in_thread"] = False
        metadata["_disable_reply_to_message"] = True
        content = intro_text.strip()
        guide = self._build_onboarding_guide_message(profile, runtime=self._prompt_context_for_message(msg))
        if guide:
            content = f"{content}\n\n{guide}" if content else guide
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=content,
            metadata=metadata,
        )

    def _build_onboarding_completed_card(
        self,
        profile: dict[str, Any] | None = None,
        runtime: PromptContext | None = None,
    ) -> str:
        onboarding_tpl = self._runtime_text.template("onboarding_form")
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "turquoise",
                "title": {
                    "tag": "plain_text",
                    "content": str(onboarding_tpl.get("completed_card_title") or "Completed"),
                },
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": self._build_onboarding_guide_message(profile, runtime=runtime).replace("\n", "  \n"),
                }
            ],
        }
        return json.dumps(card, ensure_ascii=False)

    @staticmethod
    def _is_private_feishu_message(msg: InboundMessage) -> bool:
        if msg.channel != "feishu":
            return False
        return str((msg.metadata or {}).get("chat_type") or "") != "group"

    def _mark_bootstrap_defaulted(self, profile: dict[str, Any], *, step: str) -> None:
        onboarding = cast(dict[str, Any], profile["onboarding"])
        now_iso = self._now_iso()
        onboarding.update(
            {
                "status": "completed",
                "step": step,
                "started_at": onboarding.get("started_at") or now_iso,
                "completed_at": onboarding.get("completed_at") or now_iso,
                "updated_at": now_iso,
                self._ONBOARDING_GUIDE_PROMPTED_KEY: now_iso,
            }
        )

    def _prepare_private_bootstrap_turn(
        self,
        msg: InboundMessage,
        profile: dict[str, Any],
        *,
        step: str,
        proactive: bool = False,
        reentry: bool = False,
    ) -> None:
        self._mark_bootstrap_defaulted(profile, step=step)
        self._user_memory_store.write(msg.channel, msg.sender_id, profile)

        metadata = dict(msg.metadata or {})
        metadata["_bootstrap"] = True
        metadata["_bootstrap_inline"] = not proactive and not reentry
        if proactive:
            metadata["_bootstrap_proactive"] = True
            metadata["_disable_reply_to_message"] = True
        if reentry:
            metadata["_bootstrap_reentry"] = True
            metadata["_disable_reply_to_message"] = True
        msg.metadata = metadata

    def _bootstrap_internal_prompt(self, msg: InboundMessage) -> str:
        metadata = msg.metadata or {}
        if metadata.get("_bootstrap_proactive"):
            return (
                f"{self._BOOTSTRAP_INTERNAL_TRIGGER_PREFIX}\n"
                "The user has just opened this private chat and has not sent a real message yet. "
                "Start the conversation proactively according to the current bootstrap files."
            )
        if metadata.get("_bootstrap_reentry"):
            return (
                f"{self._BOOTSTRAP_INTERNAL_TRIGGER_PREFIX}\n"
                "The user explicitly asked to revisit setup. Re-open the conversation naturally based on the current bootstrap files."
            )
        return (
            f"{self._BOOTSTRAP_INTERNAL_TRIGGER_PREFIX}\n"
            "This is the first real user message in a brand-new private chat. Bootstrap is not optional here. "
            "Respond to the user's actual message, but the reply must begin the bootstrap conversation according to the current bootstrap files. "
            "Do not skip directly to a generic help offer.\n\n"
            f"Actual user message:\n{msg.content}"
        )

    def _effective_user_message_for_llm(self, msg: InboundMessage) -> str:
        metadata = msg.metadata or {}
        if metadata.get("_bootstrap"):
            return self._bootstrap_internal_prompt(msg)
        return msg.content

    def _build_onboarding_card_outbound(
        self,
        msg: InboundMessage,
        *,
        card_payload: str,
        stage: str,
        intro_text: str,
        update_message_id: str | None = None,
    ) -> OutboundMessage:
        metadata = dict(msg.metadata or {})
        metadata["interactive_content"] = card_payload
        metadata["onboarding"] = True
        metadata["onboarding_stage"] = stage
        metadata["_reply_in_thread"] = False
        metadata["_disable_reply_to_message"] = True
        if update_message_id:
            metadata["_update_message_id"] = update_message_id
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=intro_text,
            metadata=metadata,
        )

    async def _try_handle_onboarding(
        self,
        msg: InboundMessage,
        *,
        session: Session,
        raw_cmd: str,
    ) -> OutboundMessage | None:
        if not self._onboarding_enabled_for_message(msg):
            return None

        feishu_cfg = self._feishu_onboarding_config()
        if feishu_cfg is None:
            return None

        command = raw_cmd.strip()
        lower_command = command.lower()
        reentry_commands = self._onboarding_reentry_commands(feishu_cfg)
        is_reentry = lower_command in reentry_commands

        profile = self._normalize_profile(self._user_memory_store.read(msg.channel, msg.sender_id))
        if msg.channel == "feishu" and str((msg.metadata or {}).get("source_event_type") or "") == "p2p_chat_create":
            identity = cast(dict[str, Any], profile["identity"])
            preferences = cast(dict[str, Any], profile["preferences"])
            dynamic = cast(dict[str, Any], profile["dynamic"])
            dynamic.setdefault("channel", "feishu")
            dynamic.setdefault("first_source", "p2p_chat_create")
            dynamic.setdefault("first_chat_id", msg.chat_id)
            identity.setdefault("open_id", msg.sender_id)
            preferences.setdefault("preferred_name", identity.get("name") or "")
        onboarding = profile["onboarding"]
        status = str(onboarding.get("status") or "").lower()

        is_card_action = str((msg.metadata or {}).get("msg_type") or "") == "card_action"
        action_key, action_value, form_value = self._extract_onboarding_action(msg)
        normalized_action_key = action_key.strip().lower()
        callback_message_id = str((msg.metadata or {}).get("message_id") or "").strip()
        prompt_once = bool(getattr(feishu_cfg, "onboarding_guide_once", True))
        prompted_at = str(onboarding.get(self._ONBOARDING_GUIDE_PROMPTED_KEY) or "").strip()

        if self._is_private_feishu_message(msg) and not is_card_action:
            metadata = msg.metadata or {}
            source_event_type = str(metadata.get("source_event_type") or "")
            if metadata.get("_bootstrap_proactive") or source_event_type == "p2p_chat_create":
                self._prepare_private_bootstrap_turn(msg, profile, step="bootstrap_proactive", proactive=True)
                return None
            if is_reentry:
                self._prepare_private_bootstrap_turn(msg, profile, step="bootstrap_reentry", reentry=True)
                return None
            if status == "completed":
                return None
            if prompt_once and prompted_at:
                return None
            self._prepare_private_bootstrap_turn(msg, profile, step="bootstrap_default")
            return None

        if is_reentry:
            now_iso = self._now_iso()
            if bool(getattr(feishu_cfg, "onboarding_blocking", False)):
                onboarding.update(
                    {
                        "status": "pending",
                        "step": "identity",
                        "started_at": onboarding.get("started_at") or now_iso,
                        "updated_at": now_iso,
                        self._ONBOARDING_GUIDE_PROMPTED_KEY: now_iso,
                    }
                )
            else:
                onboarding.update(
                    {
                        "status": "completed",
                        "step": "bootstrap_default",
                        "started_at": onboarding.get("started_at") or now_iso,
                        "completed_at": now_iso,
                        "updated_at": now_iso,
                        self._ONBOARDING_GUIDE_PROMPTED_KEY: now_iso,
                    }
                )
            self._user_memory_store.write(msg.channel, msg.sender_id, profile)
            if bool(getattr(feishu_cfg, "onboarding_blocking", False)):
                return self._build_onboarding_card_outbound(
                    msg,
                    card_payload=self._build_onboarding_single_card(feishu_cfg, profile),
                    stage="single",
                    intro_text=self._runtime_text.prompt_text(
                        "onboarding",
                        "intro_reentry",
                        "Welcome back, let's set up again quickly.",
                    ),
                )
            return self._build_onboarding_guide_outbound(
                msg,
                stage="guide_reentry",
                intro_text=self._runtime_text.prompt_text(
                    "onboarding",
                    "intro_reentry",
                    "已重新打开上手提示。",
                ),
                profile=profile,
            )

        onboarding_action_keys = {
            "submit_onboarding",
            "skip_onboarding",
            "start_onboarding",
            "skip_all_onboarding",
            "onboarding_submit",
            "onboarding_skip",
            "onboarding_identity_submit",
            "onboarding_identity_skip",
            "onboarding_pref_submit",
            "onboarding_pref_skip",
        }
        is_onboarding_action = (
            normalized_action_key.startswith("onboarding_")
            or normalized_action_key in onboarding_action_keys
        )

        if is_card_action and is_onboarding_action:
            if status == "completed":
                return self._build_onboarding_card_outbound(
                    msg,
                    card_payload=self._build_onboarding_completed_card(
                        profile,
                        runtime=self._prompt_context_for_message(msg),
                    ),
                    stage="completed",
                    intro_text=self._runtime_text.prompt_text(
                        "onboarding",
                        "intro_completed_reentry",
                        "Setup already completed. Send /setup to reset.",
                    ),
                    update_message_id=callback_message_id or None,
                )

            if normalized_action_key == "start_onboarding":
                onboarding.update(
                    {
                        "status": "pending",
                        "step": "identity",
                        "started_at": onboarding.get("started_at") or self._now_iso(),
                        "updated_at": self._now_iso(),
                    }
                )
                self._user_memory_store.write(msg.channel, msg.sender_id, profile)
                return self._build_onboarding_card_outbound(
                    msg,
                    card_payload=self._build_onboarding_single_card(feishu_cfg, profile),
                    stage="single",
                    intro_text=self._runtime_text.prompt_text(
                        "onboarding", "intro_start", "Let's begin setup."
                    ),
                    update_message_id=callback_message_id or None,
                )

            submit_action_keys = {
                "submit_onboarding",
                "onboarding_submit",
                "onboarding_identity_submit",
                "onboarding_pref_submit",
            }
            skip_action_keys = {
                "skip_onboarding",
                "skip_all_onboarding",
                "onboarding_skip",
                "onboarding_identity_skip",
                "onboarding_pref_skip",
            }

            if normalized_action_key in submit_action_keys:
                merged_form = {**action_value, **form_value}
                user_name = self._normalize_form_value(merged_form.get("user_name"))
                if not user_name and normalized_action_key in {"onboarding_submit", "onboarding_identity_submit"}:
                    user_name = self._normalize_form_value(merged_form.get("display_name"))

                role = self._normalize_form_value(merged_form.get("role"))
                team = self._normalize_form_value(merged_form.get("team"))
                style = self._normalize_form_value(merged_form.get("tone") or merged_form.get("response_style"))
                if style not in {"concise", "standard", "detailed"}:
                    style = ""

                write_confirm = self._normalize_form_value(
                    merged_form.get("confirm_write") or merged_form.get("write_confirm")
                )
                query_scope = self._normalize_form_value(merged_form.get("query_scope"))
                if query_scope not in {"self", "all"}:
                    query_scope = ""

                preferred_name = self._normalize_form_value(merged_form.get("preferred_name"))
                if not preferred_name and normalized_action_key in {"submit_onboarding"}:
                    preferred_name = self._normalize_form_value(merged_form.get("display_name"))

                identity = profile["identity"]
                preferences = profile["preferences"]

                resolved_name = user_name or self._normalize_form_value(identity.get("name"))
                resolved_role = role or self._normalize_form_value(identity.get("role"))
                resolved_team = team or self._normalize_form_value(identity.get("team"))
                identity["name"] = resolved_name
                identity["role"] = resolved_role
                identity["team"] = resolved_team

                if style:
                    preferences["response_style"] = style
                if preferred_name:
                    preferences["preferred_name"] = preferred_name
                if query_scope:
                    preferences["query_scope"] = query_scope

                skillspec_raw = profile.get("skillspec")
                skillspec_pref: dict[str, Any]
                if isinstance(skillspec_raw, dict):
                    skillspec_pref = cast(dict[str, Any], skillspec_raw)
                else:
                    skillspec_pref = {}

                if write_confirm in {"auto", "skip", "no", "false"}:
                    skillspec_pref["confirm_preference"] = "auto"
                elif write_confirm in {"manual", "yes", "true", "confirm"}:
                    skillspec_pref["confirm_preference"] = "manual"
                elif write_confirm:
                    skillspec_pref["confirm_preference"] = "manual"
                profile["skillspec"] = skillspec_pref

                onboarding.update(
                    {
                        "status": "completed",
                        "step": "completed",
                        "completed_at": self._now_iso(),
                        "updated_at": self._now_iso(),
                    }
                )
                self._user_memory_store.write(msg.channel, msg.sender_id, profile)
                return self._build_onboarding_card_outbound(
                    msg,
                    card_payload=self._build_onboarding_completed_card(
                        profile,
                        runtime=self._prompt_context_for_message(msg),
                    ),
                    stage="completed",
                    intro_text=self._runtime_text.prompt_text(
                        "onboarding", "intro_submit_done", "Setup completed."
                    ),
                    update_message_id=callback_message_id or None,
                )

            if normalized_action_key in skip_action_keys:
                onboarding.update(
                    {
                        "status": "completed",
                        "step": "completed",
                        "completed_at": self._now_iso(),
                        "updated_at": self._now_iso(),
                        "skip_reason": "single_skipped",
                    }
                )
                self._user_memory_store.write(msg.channel, msg.sender_id, profile)
                return self._build_onboarding_card_outbound(
                    msg,
                    card_payload=self._build_onboarding_completed_card(
                        profile,
                        runtime=self._prompt_context_for_message(msg),
                    ),
                    stage="completed",
                    intro_text=self._runtime_text.prompt_text(
                        "onboarding", "intro_skip_done", "Skipped setup. Preferences will be learned in dialogue."
                    ),
                    update_message_id=callback_message_id or None,
                )

            return None

        if status == "completed":
            return None

        if prompt_once and prompted_at:
            return None

        now_iso = self._now_iso()
        if bool(getattr(feishu_cfg, "onboarding_blocking", False)):
            onboarding.update(
                {
                    "status": "pending",
                    "step": "identity",
                    "started_at": onboarding.get("started_at") or now_iso,
                    "updated_at": now_iso,
                    self._ONBOARDING_GUIDE_PROMPTED_KEY: now_iso,
                }
            )
        else:
            onboarding.update(
                {
                    "status": "completed",
                    "step": "bootstrap_default",
                    "started_at": onboarding.get("started_at") or now_iso,
                    "completed_at": now_iso,
                    "updated_at": now_iso,
                    self._ONBOARDING_GUIDE_PROMPTED_KEY: now_iso,
                }
            )
        self._user_memory_store.write(msg.channel, msg.sender_id, profile)

        guide_outbound = self._build_onboarding_guide_outbound(
            msg,
            stage="guide",
            intro_text=self._runtime_text.prompt_text(
                "onboarding",
                "intro_first",
                "我先发你一条快速上手提示，不影响继续提问。",
            ),
            profile=profile,
        )
        if bool(getattr(feishu_cfg, "onboarding_blocking", False)):
            return self._build_onboarding_card_outbound(
                msg,
                card_payload=self._build_onboarding_single_card(feishu_cfg, profile),
                stage="single",
                intro_text=self._runtime_text.prompt_text(
                    "onboarding",
                    "intro_first",
                    "Welcome, please complete setup first.",
                ),
            )

        await self.bus.publish_outbound(guide_outbound)
        return None

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
            for tool in build_feishu_data_tools(
                self.feishu_data_config,
                workspace=self.workspace,
                state_db_path=self._state_db_path,
                sqlite_options=self._sqlite_options,
            ):
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

    def _set_tool_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        sender_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """为所有需要路由信息的工具更新上下文。"""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                setter = getattr(tool, "set_context", None)
                if callable(setter):
                    setter(channel, chat_id, *([message_id] if name == "message" else []))
        for tool_name in self.tools.tool_names:
            tool = self.tools.get(tool_name)
            if tool is not None:
                runtime_setter = getattr(tool, "set_runtime_context", None)
                if callable(runtime_setter):
                    runtime_setter(channel, chat_id, sender_id, metadata)

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
        return fallback_text

    async def _render_skillspec_with_llm(self, *, msg: InboundMessage, raw_content: str) -> str:
        content = raw_content.strip()
        if not content:
            return raw_content
        if len(content) > self._SKILLSPEC_RENDER_MAX_INPUT_CHARS:
            logger.info(
                "Skillspec LLM render skipped for {} due to large payload ({} chars)",
                msg.session_key,
                len(content),
            )
            return raw_content

        primary_prompt = (
            "你已经得到一次结构化技能执行结果，请直接用自然语言回复用户。\n"
            "要求：\n"
            "1) 只基于给定结果回答，不要再次调用工具。\n"
            "2) 不要提及内部实现（如 skillspec、tool、路由）。\n"
            "3) 结果为空时，简短说明并给出下一步建议。\n\n"
            f"用户请求：\n{msg.content}\n\n"
            f"结构化结果：\n{content}\n"
        )

        retry_prompt = (
            "把下面结果改写成给用户的简短自然语言回复。"
            "不要调用工具，不要解释内部机制。\n\n"
            f"用户请求：{msg.content}\n"
            f"结果：{content}\n"
        )

        base_messages = [
            {"role": "system", "content": self.context.build_system_prompt()},
            {"role": "user", "content": ContextBuilder._build_runtime_context(msg.channel, msg.chat_id)},
        ]

        max_tokens = max(128, min(self.max_tokens, self._SKILLSPEC_RENDER_MAX_TOKENS))
        attempts = (
            (
                "primary",
                primary_prompt,
                min(self._llm_timeout_seconds, self._skillspec_render_primary_timeout_seconds),
                self.reasoning_effort,
            ),
            (
                "retry",
                retry_prompt,
                min(self._llm_timeout_seconds, self._skillspec_render_retry_timeout_seconds),
                "low",
            ),
        )

        for index, (label, prompt, timeout_seconds, reasoning_effort) in enumerate(attempts, start=1):
            messages = [*base_messages, {"role": "user", "content": prompt}]
            llm_started = time.perf_counter()
            try:
                response = await asyncio.wait_for(
                    self.provider.chat(
                        messages=messages,
                        tools=None,
                        model=self.model,
                        temperature=self.temperature,
                        max_tokens=max_tokens,
                        reasoning_effort=reasoning_effort,
                    ),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                elapsed_ms = int((time.perf_counter() - llm_started) * 1000)
                logger.warning(
                    "Skillspec LLM render {} timed out for {} after {} ms (limit={}s)",
                    label,
                    msg.session_key,
                    elapsed_ms,
                    timeout_seconds,
                )
                continue
            except Exception as exc:
                logger.warning(
                    "Skillspec LLM render {} failed for {}: {}",
                    label,
                    msg.session_key,
                    exc,
                )
                continue

            if response.finish_reason == "error":
                logger.warning("Skillspec LLM render {} returned finish_reason=error", label)
                continue
            if response.has_tool_calls:
                logger.warning("Skillspec LLM render {} returned tool_calls, retrying", label)
                continue

            rewritten = self._strip_think(response.content)
            if rewritten:
                return rewritten

            logger.warning(
                "Skillspec LLM render {} produced empty content for {} (attempt {})",
                label,
                msg.session_key,
                index,
            )

        logger.warning("Skillspec LLM render exhausted retries for {}, fallback to raw result", msg.session_key)
        return raw_content

    @staticmethod
    def _skillspec_kind(metadata: dict[str, Any] | None) -> str:
        if not isinstance(metadata, dict):
            return ""
        return str(metadata.get("skillspec_kind") or "").strip().lower()

    def _should_rewrite_skillspec_result(self, metadata: dict[str, Any] | None) -> bool:
        kind = self._skillspec_kind(metadata)
        if kind == "query" and not bool(getattr(self.skillspec_config, "query_rewrite_enabled", False)):
            return False
        return True

    # endregion

    # region [核心调度与执行循环]

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        session_key: str = "unknown",
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

        def _start_stage_heartbeat(stage: str, started_at: float) -> asyncio.Task[None] | None:
            if self._stage_heartbeat_seconds <= 0:
                return None

            async def _heartbeat() -> None:
                while True:
                    await asyncio.sleep(self._stage_heartbeat_seconds)
                    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
                    logger.warning(
                        "Stage {} still running for session {} ({} ms elapsed)",
                        stage,
                        session_key,
                        elapsed_ms,
                    )

            return asyncio.create_task(_heartbeat())

        async def _stop_stage_heartbeat(task: asyncio.Task[None] | None) -> None:
            if task is None or task.done():
                return
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        while iteration < self.max_iterations:
            iteration += 1
            streamed_content = ""
            published_stream = False
            stream_started_at = time.monotonic()
            announced_tool_names: set[str] = set()
            answer_placeholder_task: asyncio.Task[None] | None = None
            answer_placeholder_emitted = False

            async def _on_delta(delta: str) -> None:
                nonlocal streamed_content, published_stream
                if not on_progress or not delta:
                    return
                streamed_content += delta

                if not published_stream and iteration == 1:
                    elapsed_ms = int((time.monotonic() - stream_started_at) * 1000)
                    warmup_chars = self._stream_warmup_chars
                    warmup_ms = self._stream_warmup_ms
                    if not thinking_detail_emitted and not announced_tool_names:
                        warmup_chars = self._plain_stream_warmup_chars
                        warmup_ms = self._plain_stream_warmup_ms
                    if not answer_placeholder_emitted and (
                        len(streamed_content) < warmup_chars or elapsed_ms < warmup_ms
                    ):
                        return

                await _emit_progress(streamed_content, phase="answer")
                published_stream = True

            async def _on_tool_call_name(tool_name: str) -> None:
                nonlocal thinking_detail_emitted
                name = (tool_name or "").strip()
                if not on_progress or not name or name in announced_tool_names:
                    return
                announced_tool_names.add(name)
                template = self._runtime_text.prompt_text("progress", "prepare_tool", "准备调用 {tool}")
                await _emit_progress(template.format(tool=name), phase="thinking")
                thinking_detail_emitted = True

            async def _emit_answer_placeholder() -> None:
                nonlocal answer_placeholder_emitted
                if not on_progress:
                    return
                await asyncio.sleep(self._ANSWER_PLACEHOLDER_DELAY_MS / 1000)
                if published_stream or thinking_detail_emitted:
                    return
                answer_placeholder_emitted = True
                await _emit_progress(self._answer_placeholder_text, phase="answer")

            if on_progress and iteration == 1:
                answer_placeholder_task = asyncio.create_task(_emit_answer_placeholder())

            llm_started = time.perf_counter()
            llm_heartbeat_task = _start_stage_heartbeat(f"llm:{iteration}", llm_started)
            response: LLMResponse | None = None
            try:
                response = await asyncio.wait_for(
                    self.provider.chat(
                        messages=messages,
                        tools=self.tools.get_definitions(),
                        model=self.model,
                        temperature=self.temperature,
                        max_tokens=self.max_tokens,
                        reasoning_effort=self.reasoning_effort,
                        on_delta=_on_delta if on_progress else None,
                        on_tool_call_name=_on_tool_call_name if on_progress else None,
                    ),
                    timeout=self._llm_timeout_seconds,
                )
            except asyncio.TimeoutError:
                elapsed_ms = int((time.perf_counter() - llm_started) * 1000)
                logger.error(
                    "LLM stage timed out for session {} after {} ms (limit={}s)",
                    session_key,
                    elapsed_ms,
                    self._llm_timeout_seconds,
                )
                final_content = self._runtime_text.prompt_text(
                    "progress",
                    "llm_timeout",
                    "抱歉，这次处理超时了。请重试或把问题拆小一些。",
                )
                if on_progress:
                    await _emit_progress(final_content, phase="answer")
                timings.append({
                    "stage": f"llm:{iteration}",
                    "duration_ms": elapsed_ms,
                    "timeout": True,
                })
                break
            finally:
                await _stop_stage_heartbeat(llm_heartbeat_task)
                if answer_placeholder_task and not answer_placeholder_task.done():
                    answer_placeholder_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await answer_placeholder_task

            if response is None:
                final_content = "Sorry, I encountered an unexpected model response issue."
                break

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
                        template = self._runtime_text.prompt_text(
                            "progress", "call_tool_with_args", "调用 {tool}，参数：{args}"
                        )
                        await _emit_progress(
                            template.format(tool=tool_call.name, args=args_preview),
                            phase="thinking",
                        )
                        thinking_detail_emitted = True
                    else:
                        template = self._runtime_text.prompt_text(
                            "progress", "call_tool_no_args", "正在调用 {tool} ..."
                        )
                        await _emit_progress(template.format(tool=tool_call.name), phase="thinking")
                    tool_started = time.perf_counter()
                    tool_heartbeat_task = _start_stage_heartbeat(f"tool:{tool_call.name}", tool_started)
                    try:
                        result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    finally:
                        await _stop_stage_heartbeat(tool_heartbeat_task)
                    tool_duration_ms = int((time.perf_counter() - tool_started) * 1000)
                    timings.append({
                        "stage": f"tool:{tool_call.name}",
                        "duration_ms": tool_duration_ms,
                    })
                    result_preview = self._short_text(result, limit=200)
                    if result_preview:
                        template = self._runtime_text.prompt_text(
                            "progress", "tool_result", "{tool} 结果：{result}"
                        )
                        await _emit_progress(
                            template.format(tool=tool_call.name, result=result_preview),
                            phase="thinking",
                        )
                        thinking_detail_emitted = True
                    else:
                        template = self._runtime_text.prompt_text(
                            "progress", "tool_done", "{tool} 完成，继续思考中..."
                        )
                        await _emit_progress(template.format(tool=tool_call.name), phase="thinking")
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )

                await _emit_progress(
                    self._runtime_text.prompt_text("progress", "data_ready", "已获取数据，正在整理答案..."),
                    phase="thinking",
                )
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
                    await _emit_progress(
                        self._runtime_text.prompt_text("progress", "thinking_done", "思考完成"),
                        phase="thinking_done",
                    )
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
        await self._ensure_background_workers_started()
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

    def _get_session_lock(self, session_key: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_key] = lock
        return lock

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
        """在会话级锁下处理一条消息。"""
        lock = self._get_session_lock(msg.session_key)
        request_id = str((msg.metadata or {}).get("message_id") or f"{msg.session_key}:{time.time_ns()}")
        await self._audit_sink.log_event(
            "agent_request_started",
            event_id=request_id,
            chat_id=msg.chat_id,
            message_id=str((msg.metadata or {}).get("message_id") or "") or None,
            payload={
                "channel": msg.channel,
                "session_key": msg.session_key,
                "sender_id": msg.sender_id,
                "content_preview": (msg.content or "")[:240],
            },
        )
        async with lock:
            try:
                response = await self._process_message(msg)
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
                await self._audit_sink.log_event(
                    "agent_request_finished",
                    event_id=request_id,
                    chat_id=msg.chat_id,
                    message_id=str((msg.metadata or {}).get("message_id") or "") or None,
                    payload={
                        "channel": msg.channel,
                        "session_key": msg.session_key,
                        "has_response": response is not None,
                    },
                )
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception as exc:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self._audit_sink.log_event(
                    "agent_request_error",
                    event_id=request_id,
                    chat_id=msg.chat_id,
                    message_id=str((msg.metadata or {}).get("message_id") or "") or None,
                    payload={
                        "channel": msg.channel,
                        "session_key": msg.session_key,
                        "error": str(exc),
                    },
                )
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))

    async def close_mcp(self) -> None:
        """关闭 MCP 连接。"""
        await self._stop_background_workers()
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

    async def _ensure_background_workers_started(self) -> None:
        if self._workers_started:
            return
        await self._audit_sink.start()
        await self._memory_worker.start()
        self._workers_started = True

    async def _stop_background_workers(self) -> None:
        if not self._workers_started:
            return
        self._workers_started = False
        await self._audit_sink.stop()
        await self._memory_worker.stop()
        self._sqlite.close()

    def _build_commands_help_text(self) -> str:
        """返回命令总览（简短说明）。"""
        return self._runtime_text.prompt_text("help", "commands_help_text", self._default_commands_help_text()).strip()

    @staticmethod
    def _default_commands_help_text() -> str:
        return (
            "可用命令\n"
            "- /help 或 /commands：查看命令总览\n"
            "- /status：查看当前偏好与授权状态\n"
            "- /setup：查看初始化引导\n"
            "- /connect 或 /oauth：连接飞书 OAuth\n"
            "- /session：查看会话子命令帮助\n"
            "- /new：开启新会话\n"
            "- /stop：停止当前任务\n"
            "- 继续 / 展开：查看分页剩余内容\n"
            "- 确认 <token> / 取消 <token>：确认或取消写入"
        )

    @staticmethod
    def _default_session_help_text() -> str:
        return (
            "会话命令\n"
            "- /session：查看帮助\n"
            "- /session new [标题]：在群话题中新建会话\n"
            "- /session list：列出当前聊天下会话\n"
            "- /session del <id|main>：删除指定会话"
        )

    def _build_status_text(self, msg: InboundMessage) -> str:
        profile = self._normalize_profile(self._user_memory_store.read(msg.channel, msg.sender_id))
        preferences = cast(dict[str, Any], profile["preferences"])
        skillspec = cast(dict[str, Any], profile["skillspec"])
        onboarding = cast(dict[str, Any], profile["onboarding"])

        display_name = self._resolve_profile_display_name(profile)

        style = self._normalize_form_value(preferences.get("response_style")).lower()
        style_label = {
            "concise": "简洁",
            "detailed": "详细",
        }.get(style, "标准")

        confirm_pref = self._normalize_form_value(
            skillspec.get("confirm_preference")
            or profile.get("confirm_preference")
            or profile.get("write_confirm")
        ).lower()
        if confirm_pref in {"auto", "skip", "none", "no_confirm", "no-confirm", "off", "no", "false"}:
            confirm_label = "直接写入，不用每次确认"
        else:
            confirm_label = "先确认再写入"

        query_scope = self._normalize_form_value(preferences.get("query_scope")).lower()
        scope_label = "查全部" if query_scope == "all" else "只查我参与的"

        onboarding_status = self._normalize_form_value(onboarding.get("status")).lower()
        status_label = {
            "completed": "已完成",
            "pending": "进行中",
        }.get(onboarding_status, "未设置")

        oauth_label = "未启用"
        if msg.channel == "feishu" and self._feishu_oauth_service is not None:
            token_status = self._feishu_oauth_service.get_user_token_status(msg.sender_id)
            oauth_label = {
                "active": "已连接",
                "refresh_failed": "连接异常（可临时使用）",
                "reauth_required": "需要重新授权",
                "revoked": "已失效",
                "not_connected": "未连接",
            }.get(token_status, token_status)

        return (
            "📌 当前设置\n\n"
            f"怎么称呼您：{display_name}\n"
            f"回复风格：{style_label}\n"
            f"录入数据时：{confirm_label}\n"
            f"查案件时默认范围：{scope_label}\n"
            f"引导状态：{status_label}\n"
            f"飞书授权：{oauth_label}\n\n"
            "可用快捷调整：\n"
            "- 叫我XX\n"
            "- 以后简洁点 / 以后详细点\n"
            "- 不用确认直接录入\n"
            "- /setup\n"
            "- /connect"
        )

    def _handle_connect_command(self, msg: InboundMessage) -> OutboundMessage:
        if msg.channel != "feishu":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="/connect 目前仅支持飞书频道。",
            )
        if self._feishu_oauth_service is None:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="当前环境未启用飞书 OAuth 回调服务，请联系管理员开启。",
            )

        metadata = msg.metadata or {}
        thread_id = str(metadata.get("thread_id") or metadata.get("root_id") or "").strip() or None
        try:
            auth_url = self._feishu_oauth_service.create_authorization_url(
                actor_open_id=msg.sender_id,
                chat_id=msg.chat_id,
                thread_id=thread_id,
            )
        except Exception:
            logger.exception("Failed to create Feishu OAuth authorization URL")
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="授权链接生成失败，请稍后再试。",
            )

        token_status = self._feishu_oauth_service.get_user_token_status(msg.sender_id)
        status_hint = ""
        if token_status == "active":
            status_hint = "\n\n当前检测到你已授权过，重新授权会覆盖旧令牌。"
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=(
                "请点击以下链接完成飞书授权（浏览器打开）：\n"
                f"{auth_url}"
                f"{status_hint}"
            ),
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

    def _list_pending_topic_titles(self, base_key: str) -> list[str]:
        session = self.sessions.get_or_create(base_key)
        raw = session.metadata.get(self._PENDING_TOPIC_METADATA_KEY)
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for item in raw:
            title = str(item).strip()
            if title:
                out.append(title)
        return out

    def _save_pending_topic_titles(self, base_key: str, titles: list[str]) -> None:
        session = self.sessions.get_or_create(base_key)
        session.metadata[self._PENDING_TOPIC_METADATA_KEY] = titles
        self.sessions.save(session)

    def _add_pending_topic_title(self, base_key: str, title: str) -> None:
        cleaned = title.strip()
        if not cleaned:
            return
        pending = self._list_pending_topic_titles(base_key)
        if cleaned in pending:
            return
        pending.append(cleaned)
        self._save_pending_topic_titles(base_key, pending)

    def _consume_one_pending_topic_title(self, base_key: str, current_key: str) -> None:
        if current_key == base_key or not current_key.startswith(f"{base_key}:"):
            return
        pending = self._list_pending_topic_titles(base_key)
        if not pending:
            return
        self._save_pending_topic_titles(base_key, pending[1:])

    def _handle_session_command(self, msg: InboundMessage, current_key: str, raw_cmd: str) -> OutboundMessage:
        """处理 /session 子命令。"""
        parts = raw_cmd.split()
        sub = parts[1].lower() if len(parts) > 1 else ""
        base_key = f"{msg.channel}:{msg.chat_id}"

        if sub in ("", "help"):
            content = self._runtime_text.prompt_text(
                "help",
                "session_help_text",
                self._default_session_help_text(),
            ).strip()
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

            self._add_pending_topic_title(base_key, title)

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

            pending_titles = self._list_pending_topic_titles(base_key)
            if pending_titles:
                lines.append("待激活话题：")
                for title in pending_titles:
                    lines.append(f"- {title}（待激活）")
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
        await self._ensure_background_workers_started()
        # 系统消息：从 chat_id 中解析来源 ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            self._set_tool_context(
                channel,
                chat_id,
                msg.metadata.get("message_id"),
                sender_id=msg.sender_id,
                metadata=msg.metadata,
            )
            history = session.get_history(max_messages=self.memory_window)
            prompt_context = PromptContext(purpose="chat", channel=channel, chat_id=chat_id, metadata=dict(msg.metadata or {}))
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
                runtime=prompt_context,
            )
            turn_started = time.perf_counter()
            final_content, _, all_msgs, timings = await self._run_agent_loop(messages, session_key=key)
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
        self._remember_session_runtime_metadata(session, msg)
        base_key = f"{msg.channel}:{msg.chat_id}"
        self._consume_one_pending_topic_title(base_key, key)

        # 斜杠命令 (Slash commands)
        raw_cmd = msg.content.strip()
        cmd = raw_cmd.lower()

        onboarding_outbound = await self._try_handle_onboarding(msg, session=session, raw_cmd=raw_cmd)
        if onboarding_outbound is not None:
            self.sessions.save(session)
            return onboarding_outbound

        if cmd in {"/help", "/commands"}:
            help_text = self._build_commands_help_text()
            if not help_text:
                help_text = self._build_status_text(msg)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=help_text,
            )
        if cmd == "/status":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self._build_status_text(msg),
            )
        if cmd == "/step":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self._build_status_text(msg),
            )
        if cmd in {"/connect", "/oauth"}:
            return self._handle_connect_command(msg)
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
                        temp.metadata = dict(session.metadata or {})
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
                rendered = continuation.content
                if self._should_rewrite_skillspec_result(continuation.metadata):
                    rendered = await self._render_skillspec_with_llm(msg=msg, raw_content=continuation.content)
                self.sessions.save(session)
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=rendered,
                    metadata={**(msg.metadata or {}), "_tool_turn": continuation.tool_turn},
                )
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self._runtime_text.prompt_text("pagination", "no_more_content", "没有可继续的内容了。"),
                metadata={**(msg.metadata or {}), "_tool_turn": True},
            )

        if self._skillspec_runtime:
            skillspec_result = await self._skillspec_runtime.execute_if_matched(msg, session)
            if skillspec_result.handled:
                rendered = skillspec_result.content
                if self._should_rewrite_skillspec_result(skillspec_result.metadata):
                    rendered = await self._render_skillspec_with_llm(msg=msg, raw_content=skillspec_result.content)
                self.sessions.save(session)
                outbound_chat_id = skillspec_result.reply_chat_id or msg.chat_id
                outbound_metadata = {**(msg.metadata or {}), **(skillspec_result.metadata or {})}
                if skillspec_result.reply_chat_id:
                    for key in ("message_id", "thread_id", "root_id", "parent_id", "upper_message_id"):
                        outbound_metadata.pop(key, None)
                    outbound_metadata["_reply_in_thread"] = False
                outbound_metadata["_tool_turn"] = skillspec_result.tool_turn
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=outbound_chat_id,
                    content=rendered,
                    metadata=outbound_metadata,
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

        self._set_tool_context(
            msg.channel,
            msg.chat_id,
            msg.metadata.get("message_id"),
            sender_id=msg.sender_id,
            metadata=msg.metadata,
        )
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=self.memory_window)
        prompt_context = self._prompt_context_for_message(msg, session_key=key)
        llm_user_message = self._effective_user_message_for_llm(msg)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=llm_user_message,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
            runtime=prompt_context,
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
            initial_messages,
            on_progress=on_progress or _bus_progress,
            session_key=key,
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
        await self._enqueue_memory_write(msg, final_text)

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
                if isinstance(content, str) and content.startswith(self._BOOTSTRAP_INTERNAL_TRIGGER_PREFIX):
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
        return await self._memory_store.consolidate(
            session,
            self.provider,
            self.model,
            runtime=self._prompt_context_from_session(session),
            archive_all=archive_all, memory_window=self.memory_window,
        )

    async def _enqueue_memory_write(self, msg: InboundMessage, final_text: str) -> None:
        if msg.channel != "feishu":
            return
        metadata = msg.metadata or {}
        chat_type = str(metadata.get("chat_type") or "")
        is_group = chat_type == "group"
        thread_id = str(metadata.get("thread_id") or metadata.get("root_id") or "").strip() or None

        scopes: list[MemoryScope] = []
        if is_group:
            scopes.append("chat")
            if thread_id:
                scopes.append("thread")
            flush_threshold = self._memory_flush_threshold_group
        else:
            scopes.append("user")
            flush_threshold = self._memory_flush_threshold_private

        if not scopes:
            return

        force_flush = self._should_force_memory_flush(msg.content, thread_id)

        task = MemoryTurnTask(
            channel=msg.channel,
            user_id=msg.sender_id,
            chat_id=msg.chat_id,
            thread_id=thread_id,
            user_text=msg.content,
            assistant_text=final_text,
            message_id=str(metadata.get("message_id") or "") or None,
            scopes=tuple(scopes),
            force_flush=force_flush,
            flush_threshold=flush_threshold,
        )
        await self._memory_worker.enqueue(task)

    def _should_force_memory_flush(self, content: str, thread_id: str | None) -> bool:
        if not self._memory_force_flush_on_topic_end:
            return False
        if not thread_id:
            return False
        text = " ".join(content.lower().split())
        if not text:
            return False
        for keyword in self._memory_topic_end_keywords:
            token = keyword.lower()
            if token == "done":
                if re.search(r"\bdone\b", text):
                    return True
                continue
            if token in text:
                return True
        return False

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
