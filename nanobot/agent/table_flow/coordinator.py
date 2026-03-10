from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from nanobot.agent.coordinators.base import AgentCoordinator, CoordinatorToolResult
from nanobot.agent.pending_write import (
    PENDING_WRITE_METADATA_KEY,
    coerce_pending_write_result,
    extract_json_object,
    extract_pending_write_command,
    format_pending_write_preview,
)
from nanobot.agent.table_flow.write_guard import TableWriteGuard
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.session.manager import Session

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop


TABLE_FLOW_METADATA_KEY = "table_flow"
_TABLE_FLOW_LAST_PREPARE_KEY = "table_flow_last_prepare"
_PAGE_SIZE = 5
_RETRY_RE = re.compile(r"^\s*(?:重试|retry|重新试|重新来一次)\s*$", re.IGNORECASE)
_SELECTION_RE = re.compile(r"^\s*(?:确认\s*)?(\d{1,2})\s*$", re.IGNORECASE)
_ORDINAL_INDEX = {
    "第一个": 1,
    "第1个": 1,
    "第一条": 1,
    "第1条": 1,
    "第二个": 2,
    "第2个": 2,
    "第二条": 2,
    "第2条": 2,
    "第三个": 3,
    "第3个": 3,
    "第三条": 3,
    "第3条": 3,
}
_MODIFY_HINTS = ("改", "修改", "不是", "换成", "补充", "增加", "删掉", "去掉")


class TableFlowCoordinator(AgentCoordinator):
    def __init__(self, agent: "AgentLoop") -> None:
        super().__init__(agent)
        self._guard = TableWriteGuard()

    @property
    def _loop(self) -> "AgentLoop":
        assert self._agent is not None
        return self._agent

    @staticmethod
    def _table_flow(session: Session) -> dict[str, Any]:
        value = session.metadata.get(TABLE_FLOW_METADATA_KEY)
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _last_prepare(session: Session) -> dict[str, Any]:
        value = session.metadata.get(_TABLE_FLOW_LAST_PREPARE_KEY)
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _pending_write(session: Session) -> dict[str, Any]:
        value = session.metadata.get(PENDING_WRITE_METADATA_KEY)
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _record_direct_turn(session: Session, msg: InboundMessage, assistant_content: str) -> None:
        session.add_message("user", msg.content)
        session.add_message("assistant", assistant_content)

    @staticmethod
    def _set_metadata_value(session: Session, key: str, value: dict[str, Any] | None) -> None:
        metadata = dict(session.metadata or {})
        if value:
            metadata[key] = value
        else:
            metadata.pop(key, None)
        session.metadata = metadata

    @classmethod
    def _set_table_flow(cls, session: Session, payload: dict[str, Any]) -> None:
        cls._set_metadata_value(session, TABLE_FLOW_METADATA_KEY, payload)

    @classmethod
    def _clear_table_flow(cls, session: Session) -> None:
        cls._set_metadata_value(session, TABLE_FLOW_METADATA_KEY, None)

    @classmethod
    def _set_last_prepare(cls, session: Session, payload: dict[str, Any]) -> None:
        cls._set_metadata_value(session, _TABLE_FLOW_LAST_PREPARE_KEY, payload)

    @classmethod
    def _clear_last_prepare(cls, session: Session) -> None:
        cls._set_metadata_value(session, _TABLE_FLOW_LAST_PREPARE_KEY, None)

    @staticmethod
    def _clear_result_selection(session: Session) -> None:
        metadata = dict(session.metadata or {})
        metadata.pop("result_selection", None)
        session.metadata = metadata

    @staticmethod
    def _set_result_selection(session: Session, *, kind: str, items: list[dict[str, Any]]) -> None:
        metadata = dict(session.metadata or {})
        metadata["result_selection"] = {
            "kind": kind,
            "items": [dict(item) for item in items],
            "offset": min(len(items), _PAGE_SIZE),
            "page_size": _PAGE_SIZE,
        }
        session.metadata = metadata

    @staticmethod
    def _recent_selected_table(session: Session) -> dict[str, Any]:
        value = session.metadata.get("recent_selected_table")
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _is_retry(text: str) -> bool:
        return bool(_RETRY_RE.match(text.strip()))

    @staticmethod
    def _looks_like_modify(text: str) -> bool:
        stripped = text.strip()
        return bool(stripped) and any(token in stripped for token in _MODIFY_HINTS)

    @staticmethod
    def _build_outbound(msg: InboundMessage, content: str) -> OutboundMessage:
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=content,
            metadata={**(msg.metadata or {}), "_tool_turn": True},
        )

    @staticmethod
    def _selection_index(text: str) -> int | None:
        stripped = text.strip()
        match = _SELECTION_RE.match(stripped)
        if match:
            return int(match.group(1)) - 1
        for token, index in _ORDINAL_INDEX.items():
            if token in stripped:
                return index - 1
        return None

    @classmethod
    def _select_item(cls, text: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not items:
            return None
        index = cls._selection_index(text)
        if index is not None and 0 <= index < len(items):
            return dict(items[index])
        stripped = text.strip()
        if not stripped:
            return None
        matches: list[dict[str, Any]] = []
        lowered = stripped.lower()
        for item in items:
            labels = [
                str(item.get("name") or "").strip(),
                str(item.get("table_id") or "").strip(),
                str(item.get("record_id") or "").strip(),
            ]
            labels = [label for label in labels if label]
            if any(lowered == label.lower() or lowered in label.lower() for label in labels):
                matches.append(dict(item))
        if len(matches) == 1:
            return matches[0]
        return None

    @staticmethod
    def _format_table_candidates(items: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        for idx, item in enumerate(items[:_PAGE_SIZE], start=1):
            name = str(item.get("name") or item.get("table_id") or "未命名表").strip()
            table_id = str(item.get("table_id") or "").strip()
            suffix = f" ({table_id})" if table_id else ""
            lines.append(f"- {idx}. {name}{suffix}")
        return lines

    @staticmethod
    def _format_record_candidates(items: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        for idx, item in enumerate(items[:_PAGE_SIZE], start=1):
            record_id = str(item.get("record_id") or "未命名记录").strip()
            fields_raw = item.get("fields") if isinstance(item.get("fields"), dict) else {}
            field_bits = [f"{key}: {value}" for key, value in list(fields_raw.items())[:3]]
            suffix = f" | {'; '.join(field_bits)}" if field_bits else ""
            lines.append(f"- {idx}. {record_id}{suffix}")
        return lines

    @classmethod
    def _format_confirmation_content(cls, *, kind: str, items: list[dict[str, Any]]) -> str:
        if kind == "record_confirmation":
            title = "请确认要更新哪条记录："
            lines = cls._format_record_candidates(items)
            continue_hint = "记录"
        else:
            title = "请确认要写入哪张表："
            lines = cls._format_table_candidates(items)
            continue_hint = "候选表"
        remaining = max(0, len(items) - min(len(items), _PAGE_SIZE))
        footer = ["", "直接回复编号或名称继续，回复“取消”结束。"]
        if remaining > 0:
            footer.insert(0, f"还有 {remaining} 条{continue_hint}，回复“继续”查看更多。")
        return "\n".join([title, *lines, *footer])

    @staticmethod
    def _record_confirmation_template(payload: dict[str, Any], raw_args: dict[str, Any]) -> dict[str, Any] | None:
        selected_table = payload.get("selected_table") if isinstance(payload.get("selected_table"), dict) else {}
        table_id = str(selected_table.get("table_id") or "").strip()
        if not table_id:
            return None
        draft_fields = payload.get("draft_fields") if isinstance(payload.get("draft_fields"), dict) else {}
        identity_strategy = [
            str(item).strip()
            for item in (payload.get("identity_strategy") if isinstance(payload.get("identity_strategy"), list) else [])
            if str(item).strip()
        ]
        profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
        all_identity_fields = {
            str(item).strip()
            for strategy in (profile.get("identity_strategies") if isinstance(profile.get("identity_strategies"), list) else [])
            if isinstance(strategy, list)
            for item in strategy
            if str(item).strip()
        } or set(identity_strategy)
        update_fields = {key: value for key, value in draft_fields.items() if key not in all_identity_fields}
        if not update_fields:
            update_fields = dict(draft_fields)
        arguments: dict[str, Any] = {"table_id": table_id, "fields": update_fields}
        app_token = str(raw_args.get("app_token") or "").strip()
        if app_token:
            arguments["app_token"] = app_token
        return {"tool": "bitable_update", "arguments": arguments}

    @staticmethod
    def _synthetic_request_text(tool_name: str, raw_args: dict[str, Any], preview: dict[str, Any]) -> str:
        action = str(preview.get("action") or tool_name).strip()
        table_id = str(preview.get("table_id") or raw_args.get("table_id") or "").strip()
        record_id = str(preview.get("record_id") or raw_args.get("record_id") or "").strip()
        fields = preview.get("fields") if isinstance(preview.get("fields"), dict) else raw_args.get("fields")
        if isinstance(fields, dict) and fields:
            field_text = "，".join(f"{key}:{value}" for key, value in fields.items())
            return f"{action} 表 {table_id}，字段 {field_text}".strip()
        if record_id:
            return f"{action} 表 {table_id} 的记录 {record_id}".strip()
        return f"{action} 表 {table_id}".strip()

    def _remember_prepare_context(self, session: Session, *, payload: dict[str, Any], raw_args: dict[str, Any]) -> None:
        selected_table = payload.get("selected_table") if isinstance(payload.get("selected_table"), dict) else {}
        self._set_last_prepare(
            session,
            {
                "request_text": str(payload.get("request_text") or raw_args.get("request_text") or "").strip(),
                "app_token": str(raw_args.get("app_token") or "").strip(),
                "selected_table": dict(selected_table) if selected_table else {},
            },
        )
        self._clear_result_selection(session)

    def _remember_write_context(
        self,
        session: Session,
        *,
        tool_name: str,
        raw_args: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        preview_value = payload.get("preview")
        preview = dict(preview_value) if isinstance(preview_value, dict) else {}
        last_prepare = self._last_prepare(session)
        selected_table_value = last_prepare.get("selected_table")
        selected_table = dict(selected_table_value) if isinstance(selected_table_value, dict) else {}
        if not selected_table:
            selected_table = {
                "table_id": str(preview.get("table_id") or raw_args.get("table_id") or "").strip(),
                "name": str(self._recent_selected_table(session).get("table_name") or "").strip(),
            }
        request_text = str(last_prepare.get("request_text") or "").strip()
        if not request_text:
            request_text = self._synthetic_request_text(tool_name, raw_args, preview)
        self._set_table_flow(
            session,
            {
                "kind": "write_preview",
                "tool": tool_name,
                "request_text": request_text,
                "selected_table": selected_table,
                "app_token": str(last_prepare.get("app_token") or raw_args.get("app_token") or "").strip(),
            },
        )
        self._clear_last_prepare(session)
        self._clear_result_selection(session)

    def on_tool_result(
        self,
        *,
        session: Session,
        tool_name: str,
        raw_args: dict[str, Any],
        result: str,
    ) -> CoordinatorToolResult | None:
        payload = extract_json_object(result)
        if not payload:
            return None
        if self._guard.is_prepare_tool(tool_name):
            if payload.get("needs_table_confirmation"):
                candidates = [item for item in payload.get("candidates", []) if isinstance(item, dict)]
                self._set_table_flow(
                    session,
                    {
                        "kind": "table_confirmation",
                        "request_text": str(payload.get("request_text") or raw_args.get("request_text") or "").strip(),
                        "app_token": str(raw_args.get("app_token") or "").strip(),
                        "candidates": candidates,
                    },
                )
                self._clear_last_prepare(session)
                self._set_result_selection(session, kind="table_candidates", items=candidates)
                return CoordinatorToolResult(final_content=self._format_confirmation_content(kind="table_confirmation", items=candidates))
            if payload.get("needs_record_confirmation"):
                lookup = payload.get("record_lookup") if isinstance(payload.get("record_lookup"), dict) else {}
                records = [item for item in lookup.get("records", []) if isinstance(item, dict)]
                template = self._record_confirmation_template(payload, raw_args)
                self._set_table_flow(
                    session,
                    {
                        "kind": "record_confirmation",
                        "request_text": str(payload.get("request_text") or raw_args.get("request_text") or "").strip(),
                        "app_token": str(raw_args.get("app_token") or "").strip(),
                        "records": records,
                        "write_template": template or {},
                    },
                )
                self._clear_last_prepare(session)
                self._set_result_selection(session, kind="record_candidates", items=records)
                return CoordinatorToolResult(final_content=self._format_confirmation_content(kind="record_confirmation", items=records))
            self._remember_prepare_context(session, payload=payload, raw_args=raw_args)
            return None
        if self._guard.is_table_write_tool(tool_name) and payload.get("dry_run") is True:
            self._remember_write_context(session, tool_name=tool_name, raw_args=raw_args, payload=payload)
        return None

    def _set_current_runtime(self, msg: InboundMessage, session: Session) -> None:
        exposure = self._loop._tool_exposure_context_for_message(msg, session=session)
        runtime = self._loop._turn_runtime_for_message(msg, session=session, mode=exposure.mode)
        self._loop._set_tool_context(runtime)

    async def _execute_tool(self, msg: InboundMessage, session: Session, *, tool_name: str, args: dict[str, Any]) -> str:
        self._set_current_runtime(msg, session)
        return await self._loop.tools.execute(tool_name, args)

    def _capture_result(self, *, session: Session, tool_name: str, raw_args: dict[str, Any], result: str) -> str | None:
        return self._loop._capture_coordinator_tool_result(
            session=session,
            tool_name=tool_name,
            raw_args=raw_args,
            result=result,
        )

    async def _execute_prepare_flow(
        self,
        *,
        msg: InboundMessage,
        session: Session,
        request_text: str,
        table_hint: str = "",
        app_token: str = "",
    ) -> str:
        args: dict[str, Any] = {"request_text": request_text}
        if table_hint:
            args["table_hint"] = table_hint
        if app_token:
            args["app_token"] = app_token
        result = await self._execute_tool(msg, session, tool_name="bitable_prepare_create", args=args)
        captured = self._capture_result(session=session, tool_name="bitable_prepare_create", raw_args=args, result=result)
        if captured is not None:
            return captured
        followup = self._guard.extract_prepared_followup(tool_name="bitable_prepare_create", result=result)
        if followup is None:
            return self._loop._short_text(result, limit=400) or result
        followup_args = dict(followup.arguments)
        followup_result = await self._execute_tool(msg, session, tool_name=followup.tool, args=followup_args)
        captured = self._capture_result(session=session, tool_name=followup.tool, raw_args=followup_args, result=followup_result)
        if captured is not None:
            return captured
        return self._loop._short_text(followup_result, limit=400) or followup_result

    async def _refresh_write_preview(
        self,
        *,
        msg: InboundMessage,
        session: Session,
        pending: dict[str, Any],
        refreshed: bool,
    ) -> str:
        tool_name = str(pending.get("tool") or "").strip()
        args = dict(pending.get("args") or {})
        result = await self._execute_tool(msg, session, tool_name=tool_name, args=args)
        captured = self._capture_result(session=session, tool_name=tool_name, raw_args=args, result=result)
        if captured is None:
            return self._loop._short_text(result, limit=400) or result
        if not refreshed:
            return captured
        preview = self._pending_write(session).get("preview")
        preview_payload = dict(preview) if isinstance(preview, dict) else {}
        return format_pending_write_preview(preview_payload, refreshed=True)

    async def _confirm_table_write(self, *, msg: InboundMessage, session: Session, pending: dict[str, Any]) -> str:
        tool_name = str(pending.get("tool") or "").strip()
        args = dict(pending.get("args") or {})
        token = str(pending.get("token") or "").strip()
        args["confirm_token"] = token
        result = await self._execute_tool(msg, session, tool_name=tool_name, args=args)
        payload = extract_json_object(result)
        if payload and payload.get("success") is True:
            self._loop._pending_write_coordinator._clear_pending_write(session)
            self._clear_table_flow(session)
            self._clear_last_prepare(session)
            self._clear_result_selection(session)
            return coerce_pending_write_result(payload)
        error_text = str((payload or {}).get("error") or "").strip()
        if "confirm_token" in error_text:
            return await self._refresh_write_preview(msg=msg, session=session, pending=pending, refreshed=True)
        if payload and payload.get("error"):
            return f"{coerce_pending_write_result(payload)}\n可回复“重试”重新生成预览，或直接说明新的写入要求。"
        return result

    @staticmethod
    def _table_hint_from_flow(flow: dict[str, Any], session: Session) -> str:
        selected_table = flow.get("selected_table") if isinstance(flow.get("selected_table"), dict) else {}
        if selected_table:
            return str(selected_table.get("name") or selected_table.get("table_name") or selected_table.get("table_id") or "").strip()
        recent = session.metadata.get("recent_selected_table") if isinstance(session.metadata.get("recent_selected_table"), dict) else {}
        return str(recent.get("table_name") or recent.get("name") or recent.get("table_id") or "").strip()

    async def handle(self, *, msg: InboundMessage, session: Session) -> OutboundMessage | None:
        flow = self._table_flow(session)
        pending = self._pending_write(session)
        pending_tool = str(pending.get("tool") or "").strip()
        table_pending_write = bool(pending) and self._guard.is_table_write_tool(pending_tool)
        if not flow and not table_pending_write:
            return None

        command, _token, extra_text = extract_pending_write_command(msg)
        text = msg.content.strip()

        if table_pending_write:
            if command == "cancel":
                self._loop._pending_write_coordinator._clear_pending_write(session)
                self._clear_table_flow(session)
                self._clear_last_prepare(session)
                self._clear_result_selection(session)
                content = "已取消这次待写入操作。"
                self._record_direct_turn(session, msg, content)
                self._loop.sessions.save(session)
                return self._build_outbound(msg, content)
            if command == "confirm" and not extra_text:
                content = await self._confirm_table_write(msg=msg, session=session, pending=pending)
                self._record_direct_turn(session, msg, content)
                self._loop.sessions.save(session)
                return self._build_outbound(msg, content)
            if self._is_retry(text):
                content = await self._refresh_write_preview(msg=msg, session=session, pending=pending, refreshed=True)
                self._record_direct_turn(session, msg, content)
                self._loop.sessions.save(session)
                return self._build_outbound(msg, content)
            if self._looks_like_modify(text):
                request_text = str(flow.get("request_text") or "").strip() or self._synthetic_request_text(
                    pending_tool,
                    dict(pending.get("args") or {}),
                    dict(pending.get("preview") or {}) if isinstance(pending.get("preview"), dict) else {},
                )
                content = await self._execute_prepare_flow(
                    msg=msg,
                    session=session,
                    request_text=f"{request_text}\n补充说明：{text}",
                    table_hint=self._table_hint_from_flow(flow, session),
                    app_token=str(flow.get("app_token") or "").strip(),
                )
                self._record_direct_turn(session, msg, content)
                self._loop.sessions.save(session)
                return self._build_outbound(msg, content)
            return None

        kind = str(flow.get("kind") or "").strip()
        if not kind:
            return None
        if command == "cancel":
            self._clear_table_flow(session)
            self._clear_result_selection(session)
            content = "已取消这次表格写入确认。"
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return self._build_outbound(msg, content)
        if self._is_retry(text):
            content = await self._execute_prepare_flow(
                msg=msg,
                session=session,
                request_text=str(flow.get("request_text") or "").strip(),
                app_token=str(flow.get("app_token") or "").strip(),
            )
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return self._build_outbound(msg, content)

        items_key = "records" if kind == "record_confirmation" else "candidates"
        items = [dict(item) for item in flow.get(items_key, []) if isinstance(item, dict)]
        selection = self._select_item(extra_text or text, items)
        if selection is not None:
            if kind == "record_confirmation":
                template = flow.get("write_template") if isinstance(flow.get("write_template"), dict) else {}
                tool_name = str(template.get("tool") or "bitable_update").strip() or "bitable_update"
                args = dict(template.get("arguments") or {})
                args["record_id"] = str(selection.get("record_id") or "").strip()
                result = await self._execute_tool(msg, session, tool_name=tool_name, args=args)
                content = self._capture_result(session=session, tool_name=tool_name, raw_args=args, result=result)
                if content is None:
                    content = self._loop._short_text(result, limit=400) or result
            else:
                content = await self._execute_prepare_flow(
                    msg=msg,
                    session=session,
                    request_text=str(flow.get("request_text") or "").strip(),
                    table_hint=str(selection.get("name") or selection.get("table_id") or "").strip(),
                    app_token=str(flow.get("app_token") or "").strip(),
                )
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return self._build_outbound(msg, content)

        if self._looks_like_modify(text):
            content = await self._execute_prepare_flow(
                msg=msg,
                session=session,
                request_text=f"{str(flow.get('request_text') or '').strip()}\n补充说明：{text}",
                app_token=str(flow.get("app_token") or "").strip(),
            )
            self._record_direct_turn(session, msg, content)
            self._loop.sessions.save(session)
            return self._build_outbound(msg, content)

        if command == "confirm":
            reminder = "请直接回复候选编号或名称继续，或回复“取消”结束。"
            self._record_direct_turn(session, msg, reminder)
            self._loop.sessions.save(session)
            return self._build_outbound(msg, reminder)
        return None
