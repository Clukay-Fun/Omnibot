"""SkillSpec runtime executor with query/write routing."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger

try:
    from jinja2 import Environment
except Exception:  # pragma: no cover - optional dependency fallback
    Environment = None  # type: ignore[assignment]

from nanobot.agent.skill_runtime.embedding_router import EmbeddingSkillRouter
from nanobot.agent.skill_runtime.matcher import MatchSelection, SkillSpecMatcher
from nanobot.agent.skill_runtime.output_guard import GuardResult, OutputGuard
from nanobot.agent.skill_runtime.param_parser import SkillSpecParamParser
from nanobot.agent.skill_runtime.registry import SkillSpecRegistry
from nanobot.agent.skill_runtime.reminder_runtime import ReminderRuntime
from nanobot.agent.skill_runtime.user_memory import UserMemoryStore
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage


@dataclass(slots=True)
class SkillExecutionResult:
    handled: bool
    content: str = ""
    tool_turn: bool = False
    reply_chat_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class SkillSpecExecutor:
    _SESSION_CONTINUATION_TOKEN = "skillspec_continuation_token"
    _SESSION_CONTINUATION_POLICY = "skillspec_continuation_policy"
    _SESSION_PENDING_WRITES = "skillspec_pending_writes"
    _CONTINUE_COMMANDS = {"继续", "展开"}

    def __init__(
        self,
        *,
        registry: SkillSpecRegistry,
        tools: ToolRegistry,
        output_guard: OutputGuard,
        user_memory: UserMemoryStore,
        embedding_router: EmbeddingSkillRouter | None = None,
        embedding_min_score: float = 0.15,
        route_log_enabled: bool = False,
        route_log_top_k: int = 3,
        reminder_runtime: ReminderRuntime | None = None,
    ):
        self.registry = registry
        self.tools = tools
        self.output_guard = output_guard
        self.user_memory = user_memory
        self._embedding_router = embedding_router
        self._embedding_min_score = max(0.0, float(embedding_min_score))
        self._route_log_enabled = bool(route_log_enabled)
        self._route_log_top_k = max(1, int(route_log_top_k))
        self._reminder_runtime = reminder_runtime
        self._jinja_env = Environment(trim_blocks=True, lstrip_blocks=True) if Environment is not None else None
        self.matcher = SkillSpecMatcher(
            registry.specs,
            embedding_router=self._embedding_router,
            embedding_min_score=self._embedding_min_score,
        )
        self.param_parser = SkillSpecParamParser()

    def reload(self) -> None:
        self.matcher = SkillSpecMatcher(
            self.registry.specs,
            embedding_router=self._embedding_router,
            embedding_min_score=self._embedding_min_score,
        )

    def can_handle_continuation(self, text: str) -> bool:
        return text.strip() in self._CONTINUE_COMMANDS

    def continue_from_session(self, session: Any) -> SkillExecutionResult | None:
        token = str(session.metadata.get(self._SESSION_CONTINUATION_TOKEN, "")).strip()
        if not token:
            return None
        payload = self.output_guard.continue_from(token)
        session.metadata.pop(self._SESSION_CONTINUATION_TOKEN, None)
        policy = session.metadata.get(self._SESSION_CONTINUATION_POLICY, {})
        if payload is None:
            return SkillExecutionResult(handled=True, content="没有可继续的内容了。")
        return SkillExecutionResult(
            handled=True,
            content=self._render_guarded(payload, policy=policy, session=session),
            tool_turn=True,
        )

    async def execute_if_matched(self, msg: InboundMessage, session: Any) -> SkillExecutionResult:
        confirm = await self._handle_write_confirmation(msg, session)
        if confirm:
            return confirm

        selection = self.matcher.select(msg.content)
        if not selection:
            self._log_route_miss(msg.content)
            return SkillExecutionResult(handled=False)

        session.metadata.pop(self._SESSION_CONTINUATION_TOKEN, None)
        session.metadata.pop(self._SESSION_CONTINUATION_POLICY, None)

        spec = self.registry.specs.get(selection.spec_id)
        if spec is None:
            return SkillExecutionResult(handled=False)
        route_metadata = self._build_route_metadata(selection, msg.content)

        params = self.param_parser.parse(selection.remainder, param_schema=spec.params)
        if msg.media and not params.get("paths"):
            params["paths"] = list(msg.media)
        runtime = {
            "user_context": {
                "channel": msg.channel,
                "sender_id": msg.sender_id,
                "chat_id": msg.chat_id,
            }
        }
        action = spec.action if isinstance(spec.action, dict) else {}
        kind = str(action.get("kind", "")).lower()

        if kind == "query":
            payload = await self._run_query_action(action=action, params=params, runtime=runtime)
            records = self._extract_records(payload)
            records = self._apply_soft_permission_filter(msg, records)
            if records is not None:
                if isinstance(payload, dict):
                    payload = dict(payload)
                    payload["records"] = records
            response = self._render_query_response(spec=spec, payload=payload, session=session)
            return SkillExecutionResult(
                handled=True,
                content=response,
                tool_turn=True,
                reply_chat_id=self._resolve_sensitive_reply_chat_id(spec=spec, msg=msg),
                metadata={**self._build_sensitive_metadata(spec=spec, msg=msg), **route_metadata},
            )

        if kind in {"create", "update", "delete"}:
            require_manual_confirm = self._should_require_manual_confirm(spec=spec, msg=msg)
            content = await self._run_write_dry_run(
                spec_id=selection.spec_id,
                action=action,
                params=params,
                runtime=runtime,
                session=session,
                require_manual_confirm=require_manual_confirm,
            )
            return SkillExecutionResult(handled=True, content=content, tool_turn=True, metadata=route_metadata)

        if kind in {"document_pipeline", "document"}:
            payload = await self._run_document_action(action=action, params=params, runtime=runtime)
            bridge_response = await self._run_document_write_bridge(
                spec_id=selection.spec_id,
                action=action,
                params=params,
                runtime=runtime,
                payload=payload,
                session=session,
                msg=msg,
            )
            if bridge_response is not None:
                return SkillExecutionResult(
                    handled=True,
                    content=bridge_response,
                    tool_turn=True,
                    metadata=route_metadata,
                )
            response = self._render_query_response(spec=spec, payload=payload, session=session)
            return SkillExecutionResult(
                handled=True,
                content=response,
                tool_turn=True,
                reply_chat_id=self._resolve_sensitive_reply_chat_id(spec=spec, msg=msg),
                metadata={**self._build_sensitive_metadata(spec=spec, msg=msg), **route_metadata},
            )

        if kind in {"reminder_set", "reminder_list", "reminder_cancel", "daily_summary"}:
            payload = await self._run_reminder_action(kind=kind, params=params, runtime=runtime, action=action)
            response = self._render_query_response(spec=spec, payload=payload, session=session)
            return SkillExecutionResult(handled=True, content=response, tool_turn=True, metadata=route_metadata)

        return SkillExecutionResult(handled=False)

    def _build_route_metadata(self, selection: MatchSelection, source_text: str) -> dict[str, Any]:
        route: dict[str, Any] = {
            "spec_id": selection.spec_id,
            "reason": selection.reason,
        }
        if selection.score is not None:
            route["score"] = round(float(selection.score), 6)
        metadata: dict[str, Any] = {"skillspec_route": route}
        self._log_route_hit(route)
        if self._route_log_enabled:
            metadata["skillspec_route_top_candidates"] = self._top_embedding_candidates(source_text)
        return metadata

    def _top_embedding_candidates(self, text: str) -> list[dict[str, Any]]:
        if not self._embedding_router:
            return []
        ranked = self._embedding_router.rank(text, self.registry.specs)
        top = ranked[: self._route_log_top_k]
        return [{"spec_id": spec_id, "score": round(float(score), 6)} for spec_id, score in top]

    def _log_route_hit(self, route: dict[str, Any]) -> None:
        if not self._route_log_enabled:
            return
        logger.debug(
            "Skillspec route hit spec_id={} reason={} score={}",
            route.get("spec_id"),
            route.get("reason"),
            route.get("score"),
        )

    def _log_route_miss(self, text: str) -> None:
        if not self._route_log_enabled:
            return
        candidates = self._top_embedding_candidates(text)
        logger.debug("Skillspec route miss text={} top_candidates={}", text, candidates)

    async def _run_query_action(
        self,
        *,
        action: dict[str, Any],
        params: dict[str, Any],
        runtime: dict[str, Any],
    ) -> Any:
        cross_query = action.get("cross_query")
        if isinstance(cross_query, dict):
            steps = cross_query.get("steps")
            if isinstance(steps, list):
                return await self._run_cross_query(steps=steps, params=params, runtime=runtime)

        tool_args = self._build_query_args(action, params=params, steps={}, runtime=runtime)
        return await self._execute_tool_json("bitable_search", tool_args)

    async def _run_cross_query(
        self,
        *,
        steps: list[Any],
        params: dict[str, Any],
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_id = str(step.get("id", "")).strip()
            if not step_id:
                continue
            resolved_step = self._resolve_templates(step, params=params, steps=results, runtime=runtime)
            args = self._build_query_args(resolved_step, params=params, steps=results, runtime=runtime)
            payload = await self._execute_tool_json("bitable_search", args)
            rows = self._extract_records(payload) or []
            results[step_id] = {"rows": rows, "raw": payload}
        return {"steps": results}

    def _build_query_args(
        self,
        action: dict[str, Any],
        *,
        params: dict[str, Any],
        steps: dict[str, Any],
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        args: dict[str, Any] = {}
        table = action.get("table")
        if isinstance(table, dict):
            app_token = table.get("app_token")
            table_id = table.get("table_id")
            if app_token:
                args["app_token"] = app_token
            if table_id:
                args["table_id"] = table_id

        filter_template = action.get("filter_template")
        filters: dict[str, Any] = {}
        keyword: str | None = None
        if isinstance(filter_template, dict):
            resolved = self._resolve_templates(filter_template, params=params, steps=steps, runtime=runtime)
            keyword, filters = self._parse_filter_template(resolved)

        if keyword is None:
            query = params.get("query")
            if isinstance(query, str) and query.strip():
                keyword = query.strip()
        if keyword:
            args["keyword"] = keyword
        if filters:
            args["filters"] = filters

        if isinstance(params.get("page_size"), int):
            args["limit"] = int(params["page_size"])
        return args

    async def _run_write_dry_run(
        self,
        *,
        spec_id: str,
        action: dict[str, Any],
        params: dict[str, Any],
        runtime: dict[str, Any],
        session: Any,
        require_manual_confirm: bool,
    ) -> str:
        tool_name = {
            "create": "bitable_create",
            "update": "bitable_update",
            "delete": "bitable_delete",
        }.get(str(action.get("kind", "")).lower())
        if not tool_name:
            return "技能配置错误：不支持的写入动作。"

        tool_args = self._build_write_args(action, params=params, runtime=runtime)
        payload = await self._execute_tool_json(tool_name, tool_args)
        if not isinstance(payload, dict):
            return str(payload)

        if payload.get("dry_run") is True and payload.get("confirm_token"):
            token = str(payload["confirm_token"])
            if not require_manual_confirm:
                args_with_token = dict(tool_args)
                args_with_token["confirm_token"] = token
                confirmed_payload = await self._execute_tool_json(tool_name, args_with_token)
                return self._stringify_payload(confirmed_payload)

            pending = self._pending_writes(session)
            pending[token] = {
                "spec_id": spec_id,
                "tool": tool_name,
                "args": tool_args,
            }
            session.metadata[self._SESSION_PENDING_WRITES] = pending
            preview = payload.get("preview") or {}
            return self._format_write_confirmation(preview=preview, token=token)

        return self._stringify_payload(payload)

    async def _run_document_action(
        self,
        *,
        action: dict[str, Any],
        params: dict[str, Any],
        runtime: dict[str, Any],
    ) -> Any:
        from nanobot.agent.skill_runtime.document_pipeline import process_document

        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        resolved = self._resolve_templates(args, params=params, steps={}, runtime=runtime)
        resolved_paths = resolved.get("paths") if isinstance(resolved, dict) else None
        param_paths = params.get("paths")
        paths_raw: Any = resolved_paths if isinstance(resolved_paths, list) else param_paths
        if not isinstance(paths_raw, list):
            paths_raw = []
        paths = [str(p) for p in paths_raw]

        return await process_document(
            paths=paths,
            skill_id=str((resolved or {}).get("skill_id") or action.get("skill_id") or "document"),
            user_context=runtime.get("user_context") if isinstance(runtime, dict) else None,
        )

    async def _run_document_write_bridge(
        self,
        *,
        spec_id: str,
        action: dict[str, Any],
        params: dict[str, Any],
        runtime: dict[str, Any],
        payload: Any,
        session: Any,
        msg: InboundMessage,
    ) -> str | None:
        bridge = action.get("write_bridge")
        if not isinstance(bridge, dict):
            return None

        if bridge.get("enabled") is False:
            return None
        if params.get("write_confirm") is False:
            return None

        if self._document_bridge_should_abort(payload):
            return self._format_document_bridge_error(payload)

        tool_name = str(bridge.get("tool") or "").strip()
        if not tool_name:
            return "技能配置错误：document write bridge 缺少 tool。"

        bridge_runtime = dict(runtime)
        bridge_runtime["document_result"] = payload
        bridge_runtime["result"] = payload
        args_template = bridge.get("args") if isinstance(bridge.get("args"), dict) else {}
        args = self._resolve_templates(args_template, params=params, steps={}, runtime=bridge_runtime)
        if not isinstance(args, dict):
            args = {}

        require_manual_confirm = self._should_require_bridge_manual_confirm(bridge=bridge, msg=msg)
        if not require_manual_confirm:
            result = await self._execute_tool_json(tool_name, args)
            return self._stringify_payload(result)

        token = str(bridge.get("confirm_token") or self._new_confirm_token())
        pending = self._pending_writes(session)
        pending[token] = {
            "spec_id": spec_id,
            "tool": tool_name,
            "args": args,
        }
        session.metadata[self._SESSION_PENDING_WRITES] = pending

        preview_template = bridge.get("preview")
        if preview_template is None:
            preview = payload if isinstance(payload, dict) else {"result": payload}
        else:
            preview = self._resolve_templates(preview_template, params=params, steps={}, runtime=bridge_runtime)
        return self._format_write_confirmation(preview=preview, token=token)

    def _should_require_bridge_manual_confirm(self, *, bridge: dict[str, Any], msg: InboundMessage) -> bool:
        confirm_required = bridge.get("confirm_required")
        require_manual = True if confirm_required is None else bool(confirm_required)
        if not require_manual:
            return False
        if not bool(bridge.get("confirm_respect_preference")):
            return True
        return not self._preference_allows_auto_confirm(msg)

    def _document_bridge_should_abort(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        errors = payload.get("errors")
        if not isinstance(errors, list) or not errors:
            return False
        return not self._document_bridge_has_valid_result(payload)

    def _document_bridge_has_valid_result(self, payload: dict[str, Any]) -> bool:
        results = payload.get("results")
        if not isinstance(results, list):
            return False
        for item in results:
            if not isinstance(item, dict):
                continue
            if bool(item.get("write_ready")):
                return True
            extracted_fields = item.get("extracted_fields")
            if isinstance(extracted_fields, dict) and extracted_fields:
                return True
        return False

    @staticmethod
    def _format_document_bridge_error(payload: Any) -> str:
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list):
                clean_errors = [str(item).strip() for item in errors if str(item).strip()]
                if clean_errors:
                    return f"文档提取失败：存在错误且无可写入结果。{'; '.join(clean_errors[:3])}"
        return "文档提取失败：存在错误且无可写入结果。"

    def _new_confirm_token(self) -> str:
        return uuid.uuid4().hex[:10]

    async def _run_reminder_action(
        self,
        *,
        kind: str,
        params: dict[str, Any],
        runtime: dict[str, Any],
        action: dict[str, Any],
    ) -> dict[str, Any]:
        if self._reminder_runtime is None:
            return {"error": "reminder runtime unavailable"}

        runtime_map: dict[str, Any] = runtime if isinstance(runtime, dict) else {}
        user_ctx_raw = runtime_map.get("user_context")
        user_ctx: dict[str, Any] = user_ctx_raw if isinstance(user_ctx_raw, dict) else {}
        user_id = str(user_ctx.get("sender_id") or "")
        chat_id = str(user_ctx.get("chat_id") or "")
        channel = str(user_ctx.get("channel") or "")

        if kind == "reminder_set":
            text = str(params.get("text") or params.get("query") or "").strip()
            due_at = str(params.get("due_at") or "").strip()
            if not text or not due_at:
                return {"error": "text and due_at are required"}
            calendar_requested = bool(params.get("calendar_sync") or action.get("calendar_enabled"))
            payload = await self._reminder_runtime.create_reminder(
                user_id=user_id,
                chat_id=chat_id,
                text=text,
                due_at=due_at,
                channel=channel,
                calendar_requested=calendar_requested,
            )
            if not isinstance(payload, dict) or payload.get("error"):
                return payload if isinstance(payload, dict) else {"error": "reminder_set failed"}

            reminder_raw = payload.get("reminder")
            reminder = reminder_raw if isinstance(reminder_raw, dict) else {}
            bridge_runtime = dict(runtime_map)
            bridge_runtime["reminder_result"] = payload
            bridge_runtime["reminder"] = reminder

            record_bridge = await self._run_record_bridge(
                bridge=action.get("record_bridge"),
                params=params,
                runtime=bridge_runtime,
                reminder=reminder,
            )
            calendar_bridge = await self._run_calendar_bridge(
                bridge=action.get("calendar_bridge"),
                params=params,
                runtime=bridge_runtime,
                reminder=reminder,
                calendar_requested=calendar_requested,
            )
            summary_cron_bridge = await self._run_summary_cron_bridge(
                bridge=action.get("summary_cron_bridge"),
                params=params,
                runtime=bridge_runtime,
            )

            enriched = dict(payload)
            enriched["bridges"] = {
                "record_bridge": record_bridge,
                "calendar_bridge": calendar_bridge,
                "summary_cron_bridge": summary_cron_bridge,
            }
            return enriched

        if kind == "reminder_list":
            include_cancelled = bool(params.get("include_cancelled", False))
            return self._reminder_runtime.list_reminders(user_id=user_id, include_cancelled=include_cancelled)

        if kind == "reminder_cancel":
            reminder_id = str(params.get("reminder_id") or "").strip()
            if not reminder_id:
                return {"error": "reminder_id is required"}
            return self._reminder_runtime.cancel_reminder(user_id=user_id, reminder_id=reminder_id)

        date = str(params.get("date") or datetime.now(timezone.utc).date().isoformat())
        return self._reminder_runtime.build_daily_summary(user_id=user_id, date=date)

    async def _run_record_bridge(
        self,
        *,
        bridge: Any,
        params: dict[str, Any],
        runtime: dict[str, Any],
        reminder: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(bridge, dict):
            return {"status": "skipped", "reason": "not_configured"}
        if bridge.get("enabled") is False:
            return {"status": "skipped", "reason": "disabled"}

        tool_name = str(bridge.get("tool") or "bitable_create").strip()
        if not tool_name:
            return {"status": "failed", "message": "missing tool"}
        if not self.tools.has(tool_name):
            return {"status": "unavailable", "message": f"tool '{tool_name}' not found"}

        default_args = {
            "fields": {
                "reminder_id": reminder.get("id"),
                "user_id": reminder.get("user_id"),
                "chat_id": reminder.get("chat_id"),
                "channel": reminder.get("channel"),
                "text": reminder.get("text"),
                "due_at": reminder.get("due_at"),
                "status": reminder.get("status"),
                "created_at": reminder.get("created_at"),
            }
        }
        args_template = bridge.get("args") if isinstance(bridge.get("args"), dict) else default_args
        args = self._resolve_templates(args_template, params=params, steps={}, runtime=runtime)
        if not isinstance(args, dict):
            return {"status": "failed", "message": "invalid args"}

        payload = await self._execute_bridge_tool(tool_name=tool_name, args=args)
        error = self._extract_tool_error(payload)
        if error:
            return {"status": "failed", "message": error, "result": payload}
        return {"status": "created", "result": payload}

    async def _run_calendar_bridge(
        self,
        *,
        bridge: Any,
        params: dict[str, Any],
        runtime: dict[str, Any],
        reminder: dict[str, Any],
        calendar_requested: bool,
    ) -> dict[str, Any]:
        bridge_requested = calendar_requested or bool(params.get("calendar_sync"))
        if isinstance(bridge, dict):
            bridge_requested = bridge_requested or bool(bridge.get("enabled"))
        if not bridge_requested:
            return {"status": "skipped", "reason": "not_requested"}
        if not isinstance(bridge, dict):
            return {"status": "skipped", "reason": "not_configured"}
        if bridge.get("enabled") is False:
            return {"status": "skipped", "reason": "disabled"}

        tool_name = str(bridge.get("tool") or "").strip()
        if not tool_name:
            return {"status": "failed", "message": "missing tool"}
        if not self.tools.has(tool_name):
            return {"status": "unavailable", "message": f"tool '{tool_name}' not found"}

        default_args = {
            "title": reminder.get("text", ""),
            "start_at": reminder.get("due_at", ""),
            "description": reminder.get("text", ""),
        }
        args_template = bridge.get("args") if isinstance(bridge.get("args"), dict) else default_args
        args = self._resolve_templates(args_template, params=params, steps={}, runtime=runtime)
        if not isinstance(args, dict):
            return {"status": "failed", "message": "invalid args"}

        payload = await self._execute_tool_json(tool_name, args)
        error = self._extract_tool_error(payload)
        if error:
            return {"status": "failed", "message": error, "result": payload}
        return {"status": "created", "result": payload}

    async def _run_summary_cron_bridge(
        self,
        *,
        bridge: Any,
        params: dict[str, Any],
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(bridge, dict):
            return {"status": "skipped", "reason": "not_configured"}
        if bridge.get("enabled") is False:
            return {"status": "skipped", "reason": "disabled"}
        if "summary_cron_sync" in params and not bool(params.get("summary_cron_sync")):
            return {"status": "skipped", "reason": "not_requested"}

        tool_name = str(bridge.get("tool") or "cron").strip()
        if not self.tools.has(tool_name):
            return {"status": "unavailable", "message": f"tool '{tool_name}' not found"}

        default_args = {
            "action": "add",
            "message": "daily_summary_reminder",
            "cron_expr": "0 9 * * *",
        }
        args_template = bridge.get("args") if isinstance(bridge.get("args"), dict) else default_args
        args = self._resolve_templates(args_template, params=params, steps={}, runtime=runtime)
        if not isinstance(args, dict):
            return {"status": "failed", "message": "invalid args"}
        if str(args.get("action") or "add").lower() != "add":
            return {"status": "skipped", "reason": "unsupported_action"}

        dedupe_template = bridge.get("dedupe_key_template")
        dedupe_key = ""
        if isinstance(dedupe_template, str):
            resolved_dedupe = self._resolve_templates(dedupe_template, params=params, steps={}, runtime=runtime)
            dedupe_key = str(resolved_dedupe or "").strip()
        if not dedupe_key:
            dedupe_key = str(args.get("message") or "").strip()

        if dedupe_key:
            list_payload = await self._execute_tool_json(tool_name, {"action": "list"})
            list_text = self._stringify_payload(list_payload)
            if dedupe_key in list_text:
                return {"status": "skipped", "reason": "duplicate", "dedupe_key": dedupe_key}

        payload = await self._execute_tool_json(tool_name, args)
        error = self._extract_tool_error(payload)
        if error:
            return {"status": "failed", "message": error, "result": payload}
        return {"status": "created", "result": payload}

    async def _execute_bridge_tool(self, *, tool_name: str, args: dict[str, Any]) -> Any:
        payload = await self._execute_tool_json(tool_name, args)
        if isinstance(payload, dict) and payload.get("dry_run") is True and payload.get("confirm_token"):
            confirm_args = dict(args)
            confirm_args["confirm_token"] = str(payload["confirm_token"])
            return await self._execute_tool_json(tool_name, confirm_args)
        return payload

    @staticmethod
    def _extract_tool_error(payload: Any) -> str | None:
        if isinstance(payload, str):
            stripped = payload.strip()
            if stripped.startswith("Error"):
                return stripped
            return None
        if isinstance(payload, dict):
            error = payload.get("error")
            if error not in (None, ""):
                return str(error)
        return None

    async def _handle_write_confirmation(self, msg: InboundMessage, session: Any) -> SkillExecutionResult | None:
        pending = self._pending_writes(session)
        if not pending:
            return None

        command, token = self._extract_confirm_command(msg)
        if not command or not token:
            return None
        write = pending.get(token)
        if not write:
            return SkillExecutionResult(handled=True, content=f"未找到确认令牌：{token}")

        if command == "cancel":
            pending.pop(token, None)
            session.metadata[self._SESSION_PENDING_WRITES] = pending
            return SkillExecutionResult(handled=True, content=f"已取消：{token}", tool_turn=True)

        tool_name = str(write.get("tool") or "")
        args = dict(write.get("args") or {})
        args["confirm_token"] = token
        payload = await self._execute_tool_json(tool_name, args)
        pending.pop(token, None)
        session.metadata[self._SESSION_PENDING_WRITES] = pending
        return SkillExecutionResult(handled=True, content=self._stringify_payload(payload), tool_turn=True)

    def _extract_confirm_command(self, msg: InboundMessage) -> tuple[str | None, str | None]:
        content = msg.content.strip()
        confirm_match = re.match(r"^确认\s+([a-zA-Z0-9]+)$", content)
        if confirm_match:
            return "confirm", confirm_match.group(1)
        en_confirm_match = re.match(r"^confirm\s+([a-zA-Z0-9]+)$", content, re.IGNORECASE)
        if en_confirm_match:
            return "confirm", en_confirm_match.group(1)
        cancel_match = re.match(r"^取消\s+([a-zA-Z0-9]+)$", content)
        if cancel_match:
            return "cancel", cancel_match.group(1)
        en_cancel_match = re.match(r"^cancel\s+([a-zA-Z0-9]+)$", content, re.IGNORECASE)
        if en_cancel_match:
            return "cancel", en_cancel_match.group(1)

        metadata = msg.metadata or {}
        if metadata.get("msg_type") != "card_action":
            return None, None
        action_key = str(metadata.get("action_key", "")).lower()
        if "confirm" in action_key or "确认" in action_key:
            return "confirm", self._extract_token_from_card_action(content)
        if "cancel" in action_key or "取消" in action_key:
            return "cancel", self._extract_token_from_card_action(content)
        return None, None

    def _extract_token_from_card_action(self, content: str) -> str | None:
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("action_value:") or line.startswith("form_value:"):
                _, value = line.split(":", 1)
                parsed = self._safe_json(value.strip())
                token = self._find_token(parsed)
                if token:
                    return token
        return None

    def _find_token(self, value: Any) -> str | None:
        if isinstance(value, str):
            if re.fullmatch(r"[a-zA-Z0-9]{4,128}", value):
                return value
            maybe = self._safe_json(value)
            if maybe is not value:
                return self._find_token(maybe)
            return None
        if isinstance(value, dict):
            for key in ("token", "confirm_token", "value", "id"):
                if key in value:
                    found = self._find_token(value.get(key))
                    if found:
                        return found
            for nested in value.values():
                found = self._find_token(nested)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = self._find_token(item)
                if found:
                    return found
        return None

    def _build_write_args(
        self,
        action: dict[str, Any],
        *,
        params: dict[str, Any],
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        args: dict[str, Any] = {}
        table = action.get("table")
        if isinstance(table, dict):
            if table.get("app_token"):
                args["app_token"] = table["app_token"]
            if table.get("table_id"):
                args["table_id"] = table["table_id"]

        base_args = action.get("args")
        if isinstance(base_args, dict):
            args.update(self._resolve_templates(base_args, params=params, steps={}, runtime=runtime))
        args.update(params)
        return args

    async def _execute_tool_json(self, tool_name: str, args: dict[str, Any]) -> Any:
        raw = await self.tools.execute(tool_name, args)
        if isinstance(raw, str):
            parsed = self._safe_json(raw)
            return parsed
        return raw

    def _render_query_response(self, *, spec: Any, payload: Any, session: Any) -> str:
        policy = self._resolve_output_policy(spec)
        response_cfg = spec.response if isinstance(spec.response, dict) else {}
        mapped_payload = self._apply_response_field_mapping(payload, response_cfg=response_cfg)
        rendered = self._render_payload(mapped_payload, response_cfg=response_cfg)
        if isinstance(rendered, list):
            result = self.output_guard.guard_items(rendered, max_items=int(policy.get("max_items", 5)))
            return self._persist_guard_result(result, policy=policy, session=session)

        if policy.get("max_chars"):
            result = self.output_guard.guard_text(str(rendered), max_chars=int(policy["max_chars"]))
            return self._persist_guard_result(result, policy=policy, session=session)
        return str(rendered)

    def _render_guarded(self, payload: Any, *, policy: dict[str, Any], session: Any) -> str:
        if isinstance(payload, list):
            max_items = int(policy.get("max_items") or 5)
            result = self.output_guard.guard_items(payload, max_items=max_items)
        else:
            max_chars = int(policy.get("max_chars") or 2000)
            result = self.output_guard.guard_text(str(payload), max_chars=max_chars)

        if result.truncated and result.continuation_token:
            session.metadata[self._SESSION_CONTINUATION_TOKEN] = result.continuation_token
            session.metadata[self._SESSION_CONTINUATION_POLICY] = policy
        return self._format_guard_result(result)

    def _persist_guard_result(self, result: GuardResult, *, policy: dict[str, Any], session: Any) -> str:
        if result.truncated and result.continuation_token:
            session.metadata[self._SESSION_CONTINUATION_TOKEN] = result.continuation_token
            session.metadata[self._SESSION_CONTINUATION_POLICY] = policy
        return self._format_guard_result(result)

    def _format_guard_result(self, result: GuardResult) -> str:
        if isinstance(result.content, list):
            content = "\n".join(f"{idx}. {self._stringify_row(row)}" for idx, row in enumerate(result.content, start=1))
        else:
            content = str(result.content)
        if result.truncated:
            return f"{content}\n\n回复“继续”查看剩余内容"
        return content

    def _resolve_output_policy(self, spec: Any) -> dict[str, Any]:
        response = spec.response if isinstance(spec.response, dict) else {}
        nested = response.get("output_policy") if isinstance(response, dict) else None
        policy: dict[str, Any] = {}
        if isinstance(nested, dict):
            policy.update(nested)
        root_policy = getattr(spec, "output_policy", None)
        if root_policy:
            policy.update(root_policy.model_dump(exclude_none=True))
        policy.setdefault("max_items", 5)
        return policy

    def _render_payload(self, payload: Any, *, response_cfg: dict[str, Any] | None = None) -> str | list[Any]:
        template = ""
        if isinstance(response_cfg, dict):
            template = str(response_cfg.get("template") or "").strip()

        records = self._extract_records(payload)
        if template:
            if isinstance(records, list) and self._is_record_row_template(template):
                return [self._render_record_template(template, row) for row in records]
            return self._render_template(template=template, payload=payload)

        if isinstance(payload, dict) and isinstance(payload.get("steps"), dict):
            lines: list[str] = []
            for step_id, step_data in payload["steps"].items():
                rows = step_data.get("rows") if isinstance(step_data, dict) else []
                if isinstance(rows, list):
                    lines.append(f"[{step_id}] 命中 {len(rows)} 条")
                    for row in rows[:3]:
                        lines.append(self._stringify_row(row))
            return lines or "未查询到数据。"

        if isinstance(records, list):
            if not records:
                return "未查询到数据。"
            return records

        return self._stringify_payload(payload)

    def _extract_records(self, payload: Any) -> list[Any] | None:
        if isinstance(payload, dict):
            records = payload.get("records")
            if isinstance(records, list):
                return records
        return None

    def _apply_response_field_mapping(self, payload: Any, *, response_cfg: dict[str, Any]) -> Any:
        mapping = response_cfg.get("field_mapping") if isinstance(response_cfg, dict) else None
        if not isinstance(mapping, dict) or not mapping:
            return payload

        if isinstance(payload, dict) and isinstance(payload.get("records"), list):
            updated = dict(payload)
            updated["records"] = [self._map_record_fields(record, mapping) for record in payload["records"]]
            return updated
        return payload

    def _map_record_fields(self, record: Any, mapping: dict[str, Any]) -> Any:
        if not isinstance(record, dict):
            return record

        mapped_fields: dict[str, Any] = {}
        for target, source in mapping.items():
            if not isinstance(target, str) or not target.strip():
                continue
            mapped_fields[target] = self._resolve_record_value(record, source)

        if not mapped_fields:
            return record

        out = dict(record)
        existing_fields_text_raw = out.get("fields_text")
        existing_fields_text: dict[str, Any] = (
            existing_fields_text_raw if isinstance(existing_fields_text_raw, dict) else {}
        )
        merged_fields_text = dict(existing_fields_text)
        merged_fields_text.update(mapped_fields)
        out["fields_text"] = merged_fields_text
        return out

    def _resolve_record_value(self, record: dict[str, Any], source: Any) -> Any:
        if not isinstance(source, str):
            return source

        text = source.strip()
        if not text:
            return ""

        fields_raw = record.get("fields")
        fields: dict[str, Any] = fields_raw if isinstance(fields_raw, dict) else {}
        if text in fields:
            return fields[text]

        return self._resolve_template_path(text, row=record)

    def _render_record_template(self, template: str, row: Any) -> str:
        if not isinstance(row, dict):
            return str(row)

        context = self._build_row_template_context(row)
        if self._jinja_env is not None:
            try:
                rendered = self._jinja_env.from_string(template).render(context)
                normalized = rendered.strip()
                if normalized:
                    return normalized
            except Exception:
                logger.debug("Failed to render row template via jinja2; fallback to legacy formatter")

        def _replace(found: re.Match[str]) -> str:
            resolved = self._resolve_template_path(found.group(1).strip(), row=row)
            if resolved is None:
                return ""
            if isinstance(resolved, (dict, list)):
                return json.dumps(resolved, ensure_ascii=False)
            return str(resolved)

        return re.sub(r"\{\{\s*(.*?)\s*\}\}", _replace, template)

    def _render_template(self, *, template: str, payload: Any) -> str:
        context = self._build_payload_template_context(payload)
        if self._jinja_env is not None:
            try:
                rendered = self._jinja_env.from_string(template).render(context)
                normalized = rendered.strip()
                if normalized:
                    return normalized
            except Exception:
                logger.debug("Failed to render payload template via jinja2; fallback to JSON")
        return self._stringify_payload(payload)

    def _build_payload_template_context(self, payload: Any) -> dict[str, Any]:
        context: dict[str, Any] = {
            "result": payload,
            "payload": payload,
        }
        if isinstance(payload, dict):
            context.update(payload)
            records = payload.get("records")
            if isinstance(records, list):
                context["records"] = [self._build_row_template_context(row) for row in records]
        elif isinstance(payload, list):
            rows = [self._build_row_template_context(row) for row in payload]
            context["records"] = rows
            context["items"] = rows
        return context

    @staticmethod
    def _is_record_row_template(template: str) -> bool:
        return "{%" not in template and "records" not in template and "result" not in template

    def _build_row_template_context(self, row: Any) -> Any:
        if not isinstance(row, dict):
            return row
        fields_raw = row.get("fields")
        fields: dict[str, Any] = fields_raw if isinstance(fields_raw, dict) else {}
        fields_text_raw = row.get("fields_text")
        fields_text: dict[str, Any] = fields_text_raw if isinstance(fields_text_raw, dict) else {}
        merged_fields = dict(fields)
        merged_fields.update(fields_text)
        context = dict(merged_fields)
        context["row"] = row
        context["fields"] = merged_fields
        return context

    def _resolve_template_path(self, path: str, *, row: dict[str, Any]) -> Any:
        fields = row.get("fields_text") if isinstance(row.get("fields_text"), dict) else {}
        if not fields:
            fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}

        if path.startswith("row."):
            return self._resolve_dot_path(row, path[len("row.") :])
        if path.startswith("fields."):
            return self._resolve_dot_path(fields, path[len("fields.") :])
        return self._resolve_dot_path(fields, path)

    @staticmethod
    def _resolve_dot_path(root: Any, path: str) -> Any:
        if path == "":
            return root
        cur = root
        for token in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(token)
            else:
                return None
        return cur

    def _should_require_manual_confirm(self, *, spec: Any, msg: InboundMessage) -> bool:
        response = spec.response if isinstance(spec.response, dict) else {}
        confirm_required = response.get("confirm_required")
        require_manual = True if confirm_required is None else bool(confirm_required)
        if not require_manual:
            return False
        if not bool(response.get("confirm_respect_preference")):
            return True
        return not self._preference_allows_auto_confirm(msg)

    def _preference_allows_auto_confirm(self, msg: InboundMessage) -> bool:
        profile = self.user_memory.read(msg.channel, msg.sender_id)
        candidates = [
            profile.get("confirm_preference"),
            profile.get("write_confirm"),
        ]
        skillspec_pref = profile.get("skillspec")
        if isinstance(skillspec_pref, dict):
            candidates.extend(
                [
                    skillspec_pref.get("confirm_preference"),
                    skillspec_pref.get("write_confirm"),
                ]
            )
        for value in candidates:
            if isinstance(value, bool):
                return not value
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"auto", "skip", "none", "no_confirm", "no-confirm", "off"}:
                    return True
                if normalized in {"manual", "confirm", "always", "on"}:
                    return False
        return False

    def _resolve_sensitive_reply_chat_id(self, *, spec: Any, msg: InboundMessage) -> str | None:
        response = spec.response if isinstance(spec.response, dict) else {}
        if not bool(response.get("sensitive")):
            return None
        chat_type = str((msg.metadata or {}).get("chat_type") or "")
        if chat_type != "group":
            return None
        sender_id = str(msg.sender_id or "").strip()
        return sender_id or None

    @staticmethod
    def _format_write_confirmation(*, preview: Any, token: str) -> str:
        preview_text = json.dumps(preview, ensure_ascii=False)
        return f"写入预览：{preview_text}\n确认 {token} / 取消 {token}"

    def _build_sensitive_metadata(self, *, spec: Any, msg: InboundMessage) -> dict[str, Any]:
        private_chat_id = self._resolve_sensitive_reply_chat_id(spec=spec, msg=msg)
        if not private_chat_id:
            return {}
        return {
            "private_delivery": True,
            "private_delivery_target": private_chat_id,
            "_reply_in_thread": False,
            "sensitive": True,
        }

    def _apply_soft_permission_filter(self, msg: InboundMessage, records: list[Any] | None) -> list[Any] | None:
        if records is None:
            return None
        profile = self.user_memory.read(msg.channel, msg.sender_id)
        role_text = str(profile.get("role") or profile.get("岗位") or "").lower()
        if "intern" not in role_text and "实习" not in role_text:
            return records

        visible: list[Any] = []
        for record in records:
            if self._record_visible_to_intern(record, sender_id=msg.sender_id):
                visible.append(record)
        return visible

    def _record_visible_to_intern(self, record: Any, *, sender_id: str) -> bool:
        dumped = json.dumps(record, ensure_ascii=False)
        if sender_id in dumped:
            return True
        if not isinstance(record, dict):
            return False

        fields = record.get("fields")
        if not isinstance(fields, dict):
            fields = {}
        candidates = [
            record.get("owner"),
            record.get("assignee"),
            fields.get("owner"),
            fields.get("assignee"),
            fields.get("负责人"),
            fields.get("所有者"),
        ]
        for value in candidates:
            if str(value).strip() == sender_id:
                return True
        return False

    def _resolve_templates(
        self,
        value: Any,
        *,
        params: dict[str, Any],
        steps: dict[str, Any],
        runtime: dict[str, Any],
    ) -> Any:
        if isinstance(value, str):
            return self._resolve_template_string(value, params=params, steps=steps, runtime=runtime)
        if isinstance(value, list):
            return [self._resolve_templates(item, params=params, steps=steps, runtime=runtime) for item in value]
        if isinstance(value, dict):
            resolved = {
                key: self._resolve_templates(item, params=params, steps=steps, runtime=runtime)
                for key, item in value.items()
            }
            when_expr = resolved.get("when")
            if isinstance(when_expr, str) and not self._eval_when(when_expr, params=params, steps=steps, runtime=runtime):
                return None
            return {k: v for k, v in resolved.items() if k != "when" and v is not None}
        return value

    def _resolve_template_string(
        self,
        text: str,
        *,
        params: dict[str, Any],
        steps: dict[str, Any],
        runtime: dict[str, Any],
    ) -> Any:
        match = re.fullmatch(r"\s*\{\{\s*(.*?)\s*\}\}\s*", text)
        if match:
            return self._resolve_path(match.group(1), params=params, steps=steps, runtime=runtime)

        def _replace(found: re.Match[str]) -> str:
            path = found.group(1).strip()
            resolved = self._resolve_path(path, params=params, steps=steps, runtime=runtime)
            return "" if resolved is None else str(resolved)

        return re.sub(r"\{\{\s*(.*?)\s*\}\}", _replace, text)

    def _resolve_path(
        self,
        path: str,
        *,
        params: dict[str, Any],
        steps: dict[str, Any],
        runtime: dict[str, Any],
    ) -> Any:
        root: Any
        if path.startswith("params."):
            root = params
            path = path[len("params.") :]
        elif path.startswith("steps."):
            root = steps
            path = path[len("steps.") :]
        elif path.startswith("runtime."):
            root = runtime
            path = path[len("runtime.") :]
        elif path == "result":
            return runtime.get("result") if isinstance(runtime, dict) else None
        elif path.startswith("result."):
            root = runtime.get("result") if isinstance(runtime, dict) else None
            path = path[len("result.") :]
        else:
            return None

        cur = root
        tokens = re.split(r"\.(?![^\[]*\])", path)
        for token in tokens:
            if token == "":
                continue
            key, index = self._parse_indexed_token(token)
            if isinstance(cur, dict):
                cur = cur.get(key)
            else:
                return None
            if index is not None:
                if not isinstance(cur, list) or index >= len(cur):
                    return None
                cur = cur[index]
        return cur

    @staticmethod
    def _parse_indexed_token(token: str) -> tuple[str, int | None]:
        match = re.fullmatch(r"([a-zA-Z0-9_\-]+)(?:\[(\d+)\])?", token)
        if not match:
            return token, None
        name = match.group(1)
        idx = int(match.group(2)) if match.group(2) is not None else None
        return name, idx

    def _eval_when(
        self,
        expr: str,
        *,
        params: dict[str, Any],
        steps: dict[str, Any],
        runtime: dict[str, Any],
    ) -> bool:
        text = expr.strip()
        negate = text.startswith("not ")
        path = text[4:].strip() if negate else text
        value = self._resolve_path(path, params=params, steps=steps, runtime=runtime)
        result = bool(value)
        return (not result) if negate else result

    def _parse_filter_template(self, filter_template: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        filters: dict[str, Any] = {}
        keyword: str | None = None

        conditions = filter_template.get("conditions")
        if isinstance(conditions, list):
            for cond in conditions:
                if not isinstance(cond, dict):
                    continue
                field = str(cond.get("field", "")).strip()
                op = str(cond.get("op", "eq")).strip().lower()
                value = cond.get("value")
                if value in (None, ""):
                    continue
                if op == "contains" and keyword is None:
                    keyword = str(value)
                    continue
                if field:
                    filters[field] = value
            return keyword, filters

        field = str(filter_template.get("field", "")).strip()
        value = filter_template.get("value")
        op = str(filter_template.get("op", "eq")).lower()
        if op == "contains":
            keyword = None if value is None else str(value)
        elif field and value not in (None, ""):
            filters[field] = value
        return keyword, filters

    @staticmethod
    def _stringify_row(row: Any) -> str:
        if isinstance(row, dict):
            fields = row.get("fields_text") if isinstance(row.get("fields_text"), dict) else row.get("fields")
            if isinstance(fields, dict) and fields:
                pairs = [f"{k}={v}" for k, v in list(fields.items())[:6]]
                return ", ".join(pairs)
            return json.dumps(row, ensure_ascii=False)
        return str(row)

    @staticmethod
    def _stringify_payload(payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _safe_json(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except Exception:
            return value

    def _pending_writes(self, session: Any) -> dict[str, dict[str, Any]]:
        raw = session.metadata.get(self._SESSION_PENDING_WRITES)
        return dict(raw) if isinstance(raw, dict) else {}
