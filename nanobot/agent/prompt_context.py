"""
描述: 提示词装配环境感知助手模块。
主要功能:
    - 为上下文加载器提炼出运行时的元数据快捷读取属性（如判别当前环境是否是飞书、群聊等）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

PromptPurpose = Literal["chat", "heartbeat", "bootstrap"]


@dataclass(slots=True)
class PromptContext:
    """
    用处: 跨周期的运行时元数据状态只读载体。

    功能:
        - 将动态透传进来的散列字典（Session Metadata）安全包装为具备业务语义解析的具象类（如 is_private 等方法）。
    """

    purpose: PromptPurpose = "chat"
    channel: str | None = None
    chat_id: str | None = None
    sender_id: str | None = None
    session_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def chat_type(self) -> str:
        return str(self.metadata.get("chat_type") or "")

    @property
    def is_feishu(self) -> bool:
        return self.channel == "feishu"

    @property
    def is_topic(self) -> bool:
        return bool(self.metadata.get("thread_id") or self.metadata.get("root_id") and self.metadata.get("parent_id"))

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"

    @property
    def is_private(self) -> bool:
        return not self.is_group

    @property
    def quoted_bot_summary(self) -> str:
        value = self.metadata.get("quoted_bot_summary")
        return str(value).strip() if value else ""

    @property
    def recent_selected_table(self) -> dict[str, Any]:
        value = self.metadata.get("recent_selected_table")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def recent_directory_hits(self) -> list[dict[str, Any]]:
        value = self.metadata.get("recent_directory_hits")
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    @property
    def referenced_message(self) -> dict[str, Any]:
        value = self.metadata.get("referenced_message")
        if isinstance(value, dict):
            return dict(value)
        if self.quoted_bot_summary:
            return {"summary": self.quoted_bot_summary}
        return {}

    @property
    def recent_case_objects(self) -> list[dict[str, Any]]:
        value = self.metadata.get("recent_case_objects")
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    @property
    def recent_contract_objects(self) -> list[dict[str, Any]]:
        value = self.metadata.get("recent_contract_objects")
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    @property
    def recent_weekly_plan_objects(self) -> list[dict[str, Any]]:
        value = self.metadata.get("recent_weekly_plan_objects")
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]
