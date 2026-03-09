"""Coordinator for deterministic candidate selection and conversation-frame enrichment."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from nanobot.agent.coordinators.base import AgentCoordinator, CoordinatorToolResult
from nanobot.agent.object_memory import build_object_entry, push_recent_object
from nanobot.agent.pending_write import extract_json_object
from nanobot.agent.tools.registry import ToolExposureContext
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.session.manager import Session

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop


_SELECT_RE = re.compile(r"^\s*(?:选|就选|用|第)?\s*(?P<index>[1-9]\d*|[一二三四五六七八九十两]+)\s*(?:个|张|位)?\s*$")
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


class ResultSelectionCoordinator(AgentCoordinator):
    def __init__(self, agent: "AgentLoop") -> None:
        super().__init__(agent)

    @property
    def _loop(self) -> "AgentLoop":
        assert self._agent is not None
        return self._agent

    @staticmethod
    def _record_direct_turn(session: Session, msg: InboundMessage, assistant_content: str) -> None:
        session.add_message("user", msg.content)
        session.add_message("assistant", assistant_content)

    @staticmethod
    def _parse_selection_index(text: str) -> int | None:
        match = _SELECT_RE.match(text)
        if not match:
            return None
        token = str(match.group("index") or "").strip()
        if not token:
            return None
        if token.isdigit():
            return int(token)
        if token in _CN_NUM:
            return _CN_NUM[token]
        return None

    @staticmethod
    def _set_metadata(session: Session, **updates: Any) -> None:
        metadata = dict(session.metadata or {})
        metadata.update(updates)
        session.metadata = metadata

    @staticmethod
    def _render_table_candidates(candidates: list[dict[str, Any]], *, offset: int, page_size: int) -> str:
        visible = candidates[:page_size]
        lines = ["找到多个候选表，请直接回复“第几个/第二个”等进行选择："]
        for idx, item in enumerate(visible, start=1):
            name = str(item.get("name") or item.get("table_name") or item.get("table_id") or "未命名表")
            table_id = str(item.get("table_id") or "").strip()
            score = item.get("score")
            extra = f"（{table_id}）" if table_id else ""
            if score not in (None, ""):
                extra += f" score={score}"
            lines.append(f"- {idx}. {name}{extra}")
        remaining = max(0, len(candidates) - offset)
        if remaining > 0:
            lines.append(f"\n还有 {remaining} 个候选，回复“继续”查看。")
        return "\n".join(lines)

    @staticmethod
    def _render_record_candidates(records: list[dict[str, Any]], *, offset: int, page_size: int) -> str:
        visible = records[:page_size]
        lines = ["找到多个候选记录，请直接回复“第几个/第二个”等进行选择："]
        for idx, item in enumerate(visible, start=1):
            record_id = str(item.get("record_id") or "").strip()
            fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
            field_preview = "，".join(
                f"{key}: {value}"
                for key, value in list(fields.items())[:3]
                if str(key).strip() and value not in (None, "")
            )
            extra = f"（{record_id}）" if record_id else ""
            lines.append(f"- {idx}. {field_preview or '未命名记录'}{extra}")
        remaining = max(0, len(records) - offset)
        if remaining > 0:
            lines.append(f"\n还有 {remaining} 个候选，回复“继续”查看。")
        return "\n".join(lines)

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

        if tool_name == "bitable_prepare_create":
            selected = payload.get("selected_table") if isinstance(payload.get("selected_table"), dict) else None
            if selected:
                self._set_metadata(session, recent_selected_table=dict(selected))
            object_entry = build_object_entry(
                selected_table=selected,
                profile=payload.get("profile") if isinstance(payload.get("profile"), dict) else None,
                draft_fields=payload.get("draft_fields") if isinstance(payload.get("draft_fields"), dict) else None,
                identity_strategy=payload.get("identity_strategy") if isinstance(payload.get("identity_strategy"), list) else None,
                record_lookup=payload.get("record_lookup") if isinstance(payload.get("record_lookup"), dict) else None,
                operation_guess=str(payload.get("operation_guess") or ""),
            )
            if object_entry is not None:
                metadata = push_recent_object(dict(session.metadata or {}), object_entry)
                session.metadata = metadata
            if payload.get("needs_table_confirmation") and isinstance(payload.get("candidates"), list):
                candidates = [dict(item) for item in payload.get("candidates", []) if isinstance(item, dict)]
                if candidates:
                    page_size = 5
                    self._set_metadata(
                        session,
                        result_selection={
                            "kind": "table_candidates",
                            "items": candidates,
                            "offset": min(len(candidates), page_size),
                            "page_size": page_size,
                        },
                    )
                    return CoordinatorToolResult(
                        final_content=self._render_table_candidates(
                            candidates,
                            offset=min(len(candidates), page_size),
                            page_size=page_size,
                        )
                    )
            record_lookup = payload.get("record_lookup") if isinstance(payload.get("record_lookup"), dict) else None
            if payload.get("needs_record_confirmation") and isinstance(record_lookup, dict) and isinstance(record_lookup.get("records"), list):
                records = [dict(item) for item in record_lookup.get("records", []) if isinstance(item, dict)]
                if records:
                    page_size = 5
                    self._set_metadata(
                        session,
                        result_selection={
                            "kind": "record_candidates",
                            "items": records,
                            "offset": min(len(records), page_size),
                            "page_size": page_size,
                        },
                        record_selection_action={
                            "tool": "bitable_update",
                            "app_token": str(raw_args.get("app_token") or ""),
                            "table_id": str((selected or {}).get("table_id") or ""),
                            "table_name": str((selected or {}).get("name") or ""),
                            "profile": dict(payload.get("profile") or {}) if isinstance(payload.get("profile"), dict) else {},
                            "draft_fields": dict(payload.get("draft_fields") or {}),
                            "identity_strategy": [
                                str(item).strip()
                                for item in payload.get("identity_strategy", [])
                                if str(item).strip()
                            ] if isinstance(payload.get("identity_strategy"), list) else [],
                            "request_text": str(payload.get("request_text") or raw_args.get("request_text") or ""),
                        },
                    )
                    return CoordinatorToolResult(
                        final_content=self._render_record_candidates(
                            records,
                            offset=min(len(records), page_size),
                            page_size=page_size,
                        )
                    )

        if tool_name == "bitable_match_table":
            best_match = payload.get("best_match") if isinstance(payload.get("best_match"), dict) else None
            if best_match:
                self._set_metadata(session, recent_selected_table=dict(best_match))

        if tool_name == "bitable_directory_search" and isinstance(payload.get("contacts"), list):
            contacts = [dict(item) for item in payload.get("contacts", []) if isinstance(item, dict)]
            self._set_metadata(
                session,
                recent_directory_hits=contacts,
                recent_directory_query=str(payload.get("keyword") or ""),
                recent_directory_offset=min(len(contacts), 5),
                result_selection={
                    "kind": "directory_contacts",
                    "items": contacts,
                    "offset": min(len(contacts), 5),
                    "page_size": 5,
                },
            )

        return None

    async def handle(self, *, msg: InboundMessage, session: Session) -> OutboundMessage | None:
        index = self._parse_selection_index(msg.content)
        if index is None:
            return None
        selection = session.metadata.get("result_selection") if isinstance(session.metadata.get("result_selection"), dict) else {}
        items = [dict(item) for item in selection.get("items", []) if isinstance(item, dict)] if selection else []
        if not items or index < 1 or index > len(items):
            return None

        selected = items[index - 1]
        kind = str(selection.get("kind") or "")
        if kind == "table_candidates":
            self._set_metadata(session, recent_selected_table=dict(selected))
            content = f"已选中表：{selected.get('name') or selected.get('table_name') or selected.get('table_id')}。"
        elif kind == "directory_contacts":
            self._set_metadata(session, recent_directory_hits=[dict(selected)])
            content = (
                f"已选中联系人：{selected.get('display_name') or selected.get('open_id')}"
                f"（open_id: {selected.get('open_id') or '—'}）。"
            )
        elif kind == "record_candidates":
            action = session.metadata.get("record_selection_action") if isinstance(session.metadata.get("record_selection_action"), dict) else {}
            record_id = str(selected.get("record_id") or "").strip()
            draft_fields = dict(action.get("draft_fields") or {}) if action else {}
            identity_strategy = {
                str(item).strip()
                for item in action.get("identity_strategy", [])
                if str(item).strip()
            } if action else set()
            update_fields = {key: value for key, value in draft_fields.items() if key not in identity_strategy}
            args: dict[str, Any] = {
                "record_id": record_id,
                "fields": update_fields,
            }
            table_id = str(action.get("table_id") or "").strip()
            app_token = str(action.get("app_token") or "").strip()
            if table_id:
                args["table_id"] = table_id
            if app_token:
                args["app_token"] = app_token
            tool_name = str(action.get("tool") or "bitable_update").strip() or "bitable_update"
            exposure = ToolExposureContext(
                channel=msg.channel,
                user_text=str(action.get("request_text") or msg.content or ""),
                mode="main_write_prepare",
            )
            result = await self._loop.tools.execute(tool_name, args, exposure=exposure)
            content = self._loop._capture_coordinator_tool_result(
                session=session,
                tool_name=tool_name,
                raw_args=args,
                result=result,
            ) or f"已选中记录：{record_id}。"
            self._set_metadata(
                session,
                recent_selected_record=dict(selected),
                result_selection={},
                record_selection_action={},
            )
            object_entry = build_object_entry(
                selected_table={"table_id": table_id, "name": str(action.get("table_name") or "")},
                profile=dict(action.get("profile") or {}) if isinstance(action.get("profile"), dict) else None,
                draft_fields=draft_fields,
                identity_strategy=list(identity_strategy),
                record_lookup={"records": [{"record_id": record_id}]},
                operation_guess="update_existing",
            )
            if object_entry is not None:
                session.metadata = push_recent_object(dict(session.metadata or {}), object_entry)
        else:
            return None

        self._record_direct_turn(session, msg, content)
        self._loop.sessions.save(session)
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})
