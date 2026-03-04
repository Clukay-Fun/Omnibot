"""SkillSpec runtime executor with query/write routing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from nanobot.agent.skill_runtime.matcher import SkillSpecMatcher
from nanobot.agent.skill_runtime.output_guard import GuardResult, OutputGuard
from nanobot.agent.skill_runtime.param_parser import SkillSpecParamParser
from nanobot.agent.skill_runtime.registry import SkillSpecRegistry
from nanobot.agent.skill_runtime.user_memory import UserMemoryStore
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage


@dataclass(slots=True)
class SkillExecutionResult:
    handled: bool
    content: str = ""
    tool_turn: bool = False


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
    ):
        self.registry = registry
        self.tools = tools
        self.output_guard = output_guard
        self.user_memory = user_memory
        self.matcher = SkillSpecMatcher(registry.specs)
        self.param_parser = SkillSpecParamParser()

    def reload(self) -> None:
        self.matcher = SkillSpecMatcher(self.registry.specs)

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
            return SkillExecutionResult(handled=False)

        session.metadata.pop(self._SESSION_CONTINUATION_TOKEN, None)
        session.metadata.pop(self._SESSION_CONTINUATION_POLICY, None)

        spec = self.registry.specs.get(selection.spec_id)
        if spec is None:
            return SkillExecutionResult(handled=False)

        params = self.param_parser.parse(selection.remainder, param_schema=spec.params)
        action = spec.action if isinstance(spec.action, dict) else {}
        kind = str(action.get("kind", "")).lower()

        if kind == "query":
            payload = await self._run_query_action(action=action, params=params)
            records = self._extract_records(payload)
            records = self._apply_soft_permission_filter(msg, records)
            if records is not None:
                if isinstance(payload, dict):
                    payload = dict(payload)
                    payload["records"] = records
            response = self._render_query_response(spec=spec, payload=payload, session=session)
            return SkillExecutionResult(handled=True, content=response, tool_turn=True)

        if kind in {"create", "update", "delete"}:
            content = await self._run_write_dry_run(spec_id=selection.spec_id, action=action, params=params, session=session)
            return SkillExecutionResult(handled=True, content=content, tool_turn=True)

        return SkillExecutionResult(handled=False)

    async def _run_query_action(self, *, action: dict[str, Any], params: dict[str, Any]) -> Any:
        cross_query = action.get("cross_query")
        if isinstance(cross_query, dict):
            steps = cross_query.get("steps")
            if isinstance(steps, list):
                return await self._run_cross_query(steps=steps, params=params)

        tool_args = self._build_query_args(action, params=params, steps={})
        return await self._execute_tool_json("bitable_search", tool_args)

    async def _run_cross_query(self, *, steps: list[Any], params: dict[str, Any]) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_id = str(step.get("id", "")).strip()
            if not step_id:
                continue
            resolved_step = self._resolve_templates(step, params=params, steps=results)
            args = self._build_query_args(resolved_step, params=params, steps=results)
            payload = await self._execute_tool_json("bitable_search", args)
            rows = self._extract_records(payload) or []
            results[step_id] = {"rows": rows, "raw": payload}
        return {"steps": results}

    def _build_query_args(self, action: dict[str, Any], *, params: dict[str, Any], steps: dict[str, Any]) -> dict[str, Any]:
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
            resolved = self._resolve_templates(filter_template, params=params, steps=steps)
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
        session: Any,
    ) -> str:
        tool_name = {
            "create": "bitable_create",
            "update": "bitable_update",
            "delete": "bitable_delete",
        }.get(str(action.get("kind", "")).lower())
        if not tool_name:
            return "技能配置错误：不支持的写入动作。"

        tool_args = self._build_write_args(action, params=params)
        payload = await self._execute_tool_json(tool_name, tool_args)
        if not isinstance(payload, dict):
            return str(payload)

        if payload.get("dry_run") is True and payload.get("confirm_token"):
            token = str(payload["confirm_token"])
            pending = self._pending_writes(session)
            pending[token] = {
                "spec_id": spec_id,
                "tool": tool_name,
                "args": tool_args,
            }
            session.metadata[self._SESSION_PENDING_WRITES] = pending
            preview = payload.get("preview") or {}
            preview_text = json.dumps(preview, ensure_ascii=False)
            return f"待确认写入：{preview_text}\n确认 {token}\n取消 {token}"

        return self._stringify_payload(payload)

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
        cancel_match = re.match(r"^取消\s+([a-zA-Z0-9]+)$", content)
        if cancel_match:
            return "cancel", cancel_match.group(1)

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
            if re.fullmatch(r"[a-zA-Z0-9]{8,64}", value):
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

    def _build_write_args(self, action: dict[str, Any], *, params: dict[str, Any]) -> dict[str, Any]:
        args: dict[str, Any] = {}
        table = action.get("table")
        if isinstance(table, dict):
            if table.get("app_token"):
                args["app_token"] = table["app_token"]
            if table.get("table_id"):
                args["table_id"] = table["table_id"]

        base_args = action.get("args")
        if isinstance(base_args, dict):
            args.update(self._resolve_templates(base_args, params=params, steps={}))
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
        rendered = self._render_payload(payload)
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

    def _render_payload(self, payload: Any) -> str | list[Any]:
        if isinstance(payload, dict) and isinstance(payload.get("steps"), dict):
            lines: list[str] = []
            for step_id, step_data in payload["steps"].items():
                rows = step_data.get("rows") if isinstance(step_data, dict) else []
                if isinstance(rows, list):
                    lines.append(f"[{step_id}] 命中 {len(rows)} 条")
                    for row in rows[:3]:
                        lines.append(self._stringify_row(row))
            return lines or "未查询到数据。"

        records = self._extract_records(payload)
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

    def _resolve_templates(self, value: Any, *, params: dict[str, Any], steps: dict[str, Any]) -> Any:
        if isinstance(value, str):
            return self._resolve_template_string(value, params=params, steps=steps)
        if isinstance(value, list):
            return [self._resolve_templates(item, params=params, steps=steps) for item in value]
        if isinstance(value, dict):
            resolved = {
                key: self._resolve_templates(item, params=params, steps=steps)
                for key, item in value.items()
            }
            when_expr = resolved.get("when")
            if isinstance(when_expr, str) and not self._eval_when(when_expr, params=params, steps=steps):
                return None
            return {k: v for k, v in resolved.items() if k != "when" and v is not None}
        return value

    def _resolve_template_string(self, text: str, *, params: dict[str, Any], steps: dict[str, Any]) -> Any:
        match = re.fullmatch(r"\s*\{\{\s*(.*?)\s*\}\}\s*", text)
        if match:
            return self._resolve_path(match.group(1), params=params, steps=steps)

        def _replace(found: re.Match[str]) -> str:
            path = found.group(1).strip()
            resolved = self._resolve_path(path, params=params, steps=steps)
            return "" if resolved is None else str(resolved)

        return re.sub(r"\{\{\s*(.*?)\s*\}\}", _replace, text)

    def _resolve_path(self, path: str, *, params: dict[str, Any], steps: dict[str, Any]) -> Any:
        root: Any
        if path.startswith("params."):
            root = params
            path = path[len("params.") :]
        elif path.startswith("steps."):
            root = steps
            path = path[len("steps.") :]
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

    def _eval_when(self, expr: str, *, params: dict[str, Any], steps: dict[str, Any]) -> bool:
        text = expr.strip()
        negate = text.startswith("not ")
        path = text[4:].strip() if negate else text
        value = self._resolve_path(path, params=params, steps=steps)
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
