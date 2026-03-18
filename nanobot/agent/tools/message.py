"""Message tool for sending messages to users."""

from typing import Any, Awaitable, Callable

from nanobot.agent.tools.base import Tool
from nanobot.bus.events import OutboundMessage
from nanobot.feishu.cards import FeishuCardPayload, validate_feishu_card_data


class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
    ):
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._default_message_id = default_message_id
        self._sent_in_turn: bool = False

    def set_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Set the current message context."""
        self._default_channel = channel
        self._default_chat_id = chat_id
        self._default_message_id = message_id

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    def start_turn(self) -> None:
        """Reset per-turn send tracking."""
        self._sent_in_turn = False

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return (
            "Send an additional message to another chat, another user, or a separate destination, "
            "such as notifying a different conversation or sending an extra follow-up. "
            "Do not use this for the assistant's normal reply in the current conversation turn. "
            "For proactive Feishu notifications, you can optionally send a structured `feishu_card` "
            "using one of the built-in templates instead of plain text."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to send. Optional when `feishu_card` is provided."
                },
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (telegram, discord, etc.)"
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional: target chat/user ID"
                },
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: list of file paths to attach (images, audio, documents)"
                },
                "feishu_card": {
                    "type": "object",
                    "description": "Optional Feishu Card 2.0 payload for proactive Feishu notifications. Use structured fields, not raw JSON.",
                    "properties": {
                        "template": {
                            "type": "string",
                            "enum": ["notification", "confirm"],
                            "description": "notification = one-way update, confirm = static confirmation card that expects a chat reply"
                        },
                        "title": {
                            "type": "string",
                            "description": "Short card title shown in the header"
                        },
                        "summary": {
                            "type": "string",
                            "description": "Concise summary of the situation"
                        },
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional short bullet items. Max 4 for notification, max 3 for confirm."
                        },
                        "timestamp": {
                            "type": "string",
                            "description": "Optional notification timestamp or time range"
                        },
                        "note": {
                            "type": "string",
                            "description": "Optional short notification note"
                        },
                        "confirm_prompt": {
                            "type": "string",
                            "description": "Required for confirm cards: a short prompt that the user can answer in chat"
                        },
                        "suggested_replies": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional confirm reply suggestions shown as text. Max 3."
                        },
                    },
                    "required": ["template", "title", "summary"],
                }
            },
            "required": []
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)

        content = params.get("content")
        feishu_card = params.get("feishu_card")
        has_content = isinstance(content, str) and bool(content.strip())
        if not has_content and feishu_card is None:
            errors.append("missing required content or feishu_card")

        if feishu_card is not None:
            errors.extend(validate_feishu_card_data(feishu_card))

        return errors

    async def execute(
        self,
        content: str | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        media: list[str] | None = None,
        feishu_card: dict[str, Any] | None = None,
        **kwargs: Any
    ) -> str:
        channel = channel or self._default_channel
        chat_id = chat_id or self._default_chat_id
        message_id = message_id or self._default_message_id

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        if not self._send_callback:
            return "Error: Message sending not configured"

        card_payload: FeishuCardPayload | None = None
        if feishu_card is not None:
            if channel != "feishu":
                return "Error: feishu_card is only supported for the feishu channel"
            try:
                card_payload = FeishuCardPayload.from_dict(feishu_card)
            except ValueError as exc:
                return f"Error: Invalid feishu_card payload: {exc}"

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content or "",
            media=media or [],
            metadata={
                "message_id": message_id,
            },
            feishu_card=card_payload,
        )

        try:
            await self._send_callback(msg)
            if channel == self._default_channel and chat_id == self._default_chat_id:
                self._sent_in_turn = True
            media_info = f" with {len(media)} attachments" if media else ""
            card_info = (
                f" using Feishu card template '{card_payload.template}'"
                if card_payload is not None
                else ""
            )
            return f"Message sent to {channel}:{chat_id}{card_info}{media_info}"
        except Exception as e:
            return f"Error sending message: {str(e)}"
