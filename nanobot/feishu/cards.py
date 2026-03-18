"""Structured Feishu Card 2.0 helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_TEMPLATE_NOTIFICATION = "notification"
_TEMPLATE_CONFIRM = "confirm"
_ALLOWED_TEMPLATES = {_TEMPLATE_NOTIFICATION, _TEMPLATE_CONFIRM}
_NOTIFICATION_MAX_ITEMS = 4
_CONFIRM_MAX_ITEMS = 3
_CONFIRM_MAX_SUGGESTED_REPLIES = 3


@dataclass(slots=True)
class FeishuCardPayload:
    """Structured params for a Feishu Card 2.0 message."""

    template: str
    title: str
    summary: str
    items: list[str] = field(default_factory=list)
    timestamp: str | None = None
    note: str | None = None
    confirm_prompt: str | None = None
    suggested_replies: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FeishuCardPayload":
        """Validate and normalize a tool/runtime dict into a payload object."""
        errors = validate_feishu_card_data(data)
        if errors:
            raise ValueError("; ".join(errors))

        items = _clean_string_list(data.get("items"))
        replies = _clean_string_list(data.get("suggested_replies"))

        return cls(
            template=str(data.get("template") or "").strip(),
            title=str(data.get("title") or "").strip(),
            summary=str(data.get("summary") or "").strip(),
            items=items,
            timestamp=_clean_optional_string(data.get("timestamp")),
            note=_clean_optional_string(data.get("note")),
            confirm_prompt=_clean_optional_string(data.get("confirm_prompt")),
            suggested_replies=replies,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the payload for transport/debugging."""
        data: dict[str, Any] = {
            "template": self.template,
            "title": self.title,
            "summary": self.summary,
        }
        if self.items:
            data["items"] = list(self.items)
        if self.timestamp:
            data["timestamp"] = self.timestamp
        if self.note:
            data["note"] = self.note
        if self.confirm_prompt:
            data["confirm_prompt"] = self.confirm_prompt
        if self.suggested_replies:
            data["suggested_replies"] = list(self.suggested_replies)
        return data


def validate_feishu_card_data(data: Any, *, path: str = "feishu_card") -> list[str]:
    """Validate structured Feishu card params."""
    if not isinstance(data, dict):
        return [f"{path} should be object"]

    errors: list[str] = []
    template = _clean_optional_string(data.get("template"))
    title = _clean_optional_string(data.get("title"))
    summary = _clean_optional_string(data.get("summary"))

    if not template:
        errors.append(f"missing required {path}.template")
    elif template not in _ALLOWED_TEMPLATES:
        errors.append(f"{path}.template must be one of {sorted(_ALLOWED_TEMPLATES)}")

    if not title:
        errors.append(f"missing required {path}.title")
    if not summary:
        errors.append(f"missing required {path}.summary")

    errors.extend(_validate_optional_string_list(data.get("items"), f"{path}.items"))
    errors.extend(_validate_optional_string(data.get("timestamp"), f"{path}.timestamp"))
    errors.extend(_validate_optional_string(data.get("note"), f"{path}.note"))
    errors.extend(_validate_optional_string(data.get("confirm_prompt"), f"{path}.confirm_prompt"))
    errors.extend(
        _validate_optional_string_list(data.get("suggested_replies"), f"{path}.suggested_replies")
    )

    if template == _TEMPLATE_NOTIFICATION:
        item_count = len(_clean_string_list(data.get("items")))
        if item_count > _NOTIFICATION_MAX_ITEMS:
            errors.append(
                f"{path}.items must have at most {_NOTIFICATION_MAX_ITEMS} entries for notification"
            )
        if _clean_optional_string(data.get("confirm_prompt")):
            errors.append(f"{path}.confirm_prompt is not allowed for notification")
        if _clean_string_list(data.get("suggested_replies")):
            errors.append(f"{path}.suggested_replies is not allowed for notification")

    if template == _TEMPLATE_CONFIRM:
        item_count = len(_clean_string_list(data.get("items")))
        reply_count = len(_clean_string_list(data.get("suggested_replies")))
        if item_count > _CONFIRM_MAX_ITEMS:
            errors.append(f"{path}.items must have at most {_CONFIRM_MAX_ITEMS} entries for confirm")
        if not _clean_optional_string(data.get("confirm_prompt")):
            errors.append(f"missing required {path}.confirm_prompt")
        if reply_count > _CONFIRM_MAX_SUGGESTED_REPLIES:
            errors.append(
                f"{path}.suggested_replies must have at most {_CONFIRM_MAX_SUGGESTED_REPLIES} entries for confirm"
            )
        if _clean_optional_string(data.get("timestamp")):
            errors.append(f"{path}.timestamp is not allowed for confirm")
        if _clean_optional_string(data.get("note")):
            errors.append(f"{path}.note is not allowed for confirm")

    return errors


def build_feishu_card(card: FeishuCardPayload) -> dict[str, Any]:
    """Build a Feishu Card 2.0 payload from structured params."""
    if card.template == _TEMPLATE_NOTIFICATION:
        return _build_notification_card(card)
    if card.template == _TEMPLATE_CONFIRM:
        return _build_confirm_card(card)
    raise ValueError(f"Unsupported Feishu card template: {card.template}")


def render_feishu_card_fallback(card: FeishuCardPayload) -> str:
    """Render a structured fallback that can be downgraded to post/text."""
    if card.template == _TEMPLATE_NOTIFICATION:
        return _render_notification_fallback(card)
    if card.template == _TEMPLATE_CONFIRM:
        return _render_confirm_fallback(card)
    raise ValueError(f"Unsupported Feishu card template: {card.template}")


def _build_notification_card(card: FeishuCardPayload) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": card.summary},
    ]
    if card.items:
        bullet_lines = "\n".join(f"- {item}" for item in card.items)
        elements.append({"tag": "markdown", "content": bullet_lines})
    if card.timestamp:
        elements.append({"tag": "markdown", "content": f"**Time:** {card.timestamp}"})
    if card.note:
        elements.append({"tag": "markdown", "content": f"**Note:** {card.note}"})
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": card.title},
        },
        "body": {
            "direction": "vertical",
            "elements": elements,
        },
    }


def _build_confirm_card(card: FeishuCardPayload) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**Current Situation:** {card.summary}"},
    ]
    if card.items:
        bullet_lines = "\n".join(f"- {item}" for item in card.items)
        elements.append({"tag": "markdown", "content": bullet_lines})

    prompt_lines = [f"**Reply In Chat:** {card.confirm_prompt or ''}"]
    if card.suggested_replies:
        prompt_lines.append(f"**Suggested Replies:** {' / '.join(card.suggested_replies)}")
    elements.append({"tag": "markdown", "content": "\n\n".join(prompt_lines)})

    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": card.title},
        },
        "body": {
            "direction": "vertical",
            "elements": elements,
        },
    }


def _render_notification_fallback(card: FeishuCardPayload) -> str:
    lines = [f"**{card.title}**", "", f"**Summary:** {card.summary}"]
    if card.items:
        lines.append("")
        lines.extend(f"- {item}" for item in card.items)
    if card.timestamp:
        lines.extend(["", f"**Time:** {card.timestamp}"])
    if card.note:
        lines.extend(["", f"**Note:** {card.note}"])
    return "\n".join(lines).strip()


def _render_confirm_fallback(card: FeishuCardPayload) -> str:
    lines = [f"**{card.title}**", "", f"**Current Situation:** {card.summary}"]
    if card.items:
        lines.append("")
        lines.extend(f"- {item}" for item in card.items)
    lines.extend(["", f"**Reply In Chat:** {card.confirm_prompt or ''}"])
    if card.suggested_replies:
        lines.extend(["", f"**Suggested Replies:** {' / '.join(card.suggested_replies)}"])
    return "\n".join(lines).strip()


def _validate_optional_string(value: Any, path: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, str):
        return [f"{path} should be string"]
    if not value.strip():
        return [f"{path} must not be empty"]
    return []


def _validate_optional_string_list(value: Any, path: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        return [f"{path} should be array"]
    errors: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            errors.append(f"{path}[{index}] should be string")
        elif not item.strip():
            errors.append(f"{path}[{index}] must not be empty")
    return errors


def _clean_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


__all__ = [
    "FeishuCardPayload",
    "build_feishu_card",
    "render_feishu_card_fallback",
    "validate_feishu_card_data",
]
