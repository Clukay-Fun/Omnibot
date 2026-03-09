"""Coordinator for deterministic candidate selection and conversation-frame enrichment."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from nanobot.agent.coordinators.base import AgentCoordinator, CoordinatorToolResult
from nanobot.agent.pending_write import extract_json_object
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
        else:
            return None

        self._record_direct_turn(session, msg, content)
        self._loop.sessions.save(session)
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata={**(msg.metadata or {}), "_tool_turn": True})
