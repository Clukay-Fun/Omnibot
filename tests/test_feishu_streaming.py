import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.feishu import FeishuChannel
from nanobot.config.schema import FeishuConfig


class _FakeResponse:
    def __init__(self, success: bool = True, data: object | None = None) -> None:
        self._success = success
        self.data = data
        self.code = 0 if success else 1
        self.msg = "ok" if success else "failed"

    def success(self) -> bool:
        return self._success

    def get_log_id(self) -> str:
        return "log-id"


class _FakeMessageAPI:
    def __init__(
        self,
        *,
        update_handler: Callable[[Any], _FakeResponse] | None = None,
        patch_handler: Callable[[Any], _FakeResponse] | None = None,
    ) -> None:
        self.create_calls: list[object] = []
        self.reply_calls: list[object] = []
        self.update_calls: list[object] = []
        self.patch_calls: list[object] = []
        self.delete_calls: list[object] = []
        self._update_handler = update_handler
        self._patch_handler = patch_handler
        self._sent_count = 0

    def _next_message_id(self) -> str:
        self._sent_count += 1
        return f"bot-{self._sent_count}"

    def create(self, request: object) -> _FakeResponse:
        self.create_calls.append(request)
        message_id = self._next_message_id()
        return _FakeResponse(True, SimpleNamespace(message_id=message_id))

    def reply(self, request: object) -> _FakeResponse:
        self.reply_calls.append(request)
        message_id = self._next_message_id()
        return _FakeResponse(True, SimpleNamespace(message_id=message_id))

    def update(self, request: object) -> _FakeResponse:
        self.update_calls.append(request)
        if self._update_handler:
            return self._update_handler(request)
        return _FakeResponse(True)

    def patch(self, request: object) -> _FakeResponse:
        self.patch_calls.append(request)
        if self._patch_handler:
            return self._patch_handler(request)
        return _FakeResponse(True)

    def delete(self, request: object) -> _FakeResponse:
        self.delete_calls.append(request)
        return _FakeResponse(True)


class _FakeCardAPI:
    def __init__(self, *, update_success: bool = True) -> None:
        self.id_convert_calls: list[object] = []
        self.settings_calls: list[object] = []
        self.update_calls: list[object] = []
        self._update_success = update_success

    def id_convert(self, request: Any) -> _FakeResponse:
        self.id_convert_calls.append(request)
        message_id = request.request_body.message_id
        return _FakeResponse(True, SimpleNamespace(card_id=f"card-{message_id}"))

    def settings(self, request: object) -> _FakeResponse:
        self.settings_calls.append(request)
        return _FakeResponse(True)

    def update(self, request: object) -> _FakeResponse:
        self.update_calls.append(request)
        return _FakeResponse(self._update_success)


class _FakeCardElementAPI:
    def __init__(
        self,
        *,
        content_success: bool = True,
        content_handler: Callable[[Any], _FakeResponse] | None = None,
    ) -> None:
        self.content_calls: list[object] = []
        self._content_success = content_success
        self._content_handler = content_handler

    def content(self, request: object) -> _FakeResponse:
        self.content_calls.append(request)
        if self._content_handler:
            return self._content_handler(request)
        return _FakeResponse(self._content_success)


def _build_channel(
    *,
    card_update_success: bool = True,
    element_update_success: bool = True,
    element_content_handler: Callable[[Any], _FakeResponse] | None = None,
    ttl_seconds: int = 600,
    min_update_ms: int = 0,
    reply_to_message: bool = True,
    show_thinking: bool = True,
    update_handler: Callable[[Any], _FakeResponse] | None = None,
    patch_handler: Callable[[Any], _FakeResponse] | None = None,
) -> tuple[FeishuChannel, SimpleNamespace]:
    config = FeishuConfig(
        stream_card_enabled=True,
        stream_card_min_update_ms=min_update_ms,
        stream_card_ttl_seconds=ttl_seconds,
        reply_to_message=reply_to_message,
        stream_card_show_thinking=show_thinking,
    )
    channel = FeishuChannel(config=config, bus=MessageBus())
    message_api = _FakeMessageAPI(update_handler=update_handler, patch_handler=patch_handler)
    card_api = _FakeCardAPI(update_success=card_update_success)
    card_element_api = _FakeCardElementAPI(
        content_success=element_update_success,
        content_handler=element_content_handler,
    )
    client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=message_api)),
        cardkit=SimpleNamespace(v1=SimpleNamespace(card=card_api, card_element=card_element_api)),
    )
    channel._client = client
    return channel, client


def _sent_message_count(client: SimpleNamespace) -> int:
    return len(client.im.v1.message.create_calls) + len(client.im.v1.message.reply_calls)


def _first_sent_card(client: SimpleNamespace) -> dict[str, Any]:
    request = client.im.v1.message.reply_calls[0] if client.im.v1.message.reply_calls else client.im.v1.message.create_calls[0]
    payload = getattr(getattr(request, "request_body", None), "content", "{}")
    return json.loads(payload)


@pytest.mark.asyncio
async def test_streaming_creates_once_then_updates_and_finalizes_once() -> None:
    channel, client = _build_channel()

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="step-1",
            metadata={"_progress": True, "message_id": "src-1"},
        )
    )
    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="step-2",
            metadata={"_progress": True, "message_id": "src-1"},
        )
    )
    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="final",
            metadata={"message_id": "src-1"},
        )
    )

    assert _sent_message_count(client) == 1
    assert len(client.cardkit.v1.card.id_convert_calls) == 1
    assert len(client.cardkit.v1.card.settings_calls) == 0
    assert len(client.cardkit.v1.card_element.content_calls) == 2
    assert len(client.cardkit.v1.card.update_calls) == 0
    assert len(client.im.v1.message.update_calls) == 0
    assert "src-1" in channel._stream_states


@pytest.mark.asyncio
async def test_streaming_prefers_reply_for_initial_message_when_enabled() -> None:
    channel, client = _build_channel(reply_to_message=True)

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="step-1",
            metadata={"_progress": True, "message_id": "src-reply"},
        )
    )

    assert len(client.im.v1.message.reply_calls) == 1
    assert len(client.im.v1.message.create_calls) == 0


@pytest.mark.asyncio
async def test_reply_defaults_to_in_chat_not_thread() -> None:
    channel, client = _build_channel(reply_to_message=True)

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="step-1",
            metadata={"_progress": True, "message_id": "src-reply-direct"},
        )
    )

    assert len(client.im.v1.message.reply_calls) == 1
    request = client.im.v1.message.reply_calls[0]
    assert getattr(getattr(request, "request_body", None), "reply_in_thread", None) is False


@pytest.mark.asyncio
async def test_thread_metadata_forces_reply_in_thread() -> None:
    channel, client = _build_channel(reply_to_message=True)

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="oc_group",
            content="step-1",
            metadata={
                "_progress": True,
                "message_id": "src-reply-thread",
                "thread_id": "th_1",
            },
        )
    )

    assert len(client.im.v1.message.reply_calls) == 1
    request = client.im.v1.message.reply_calls[0]
    assert getattr(getattr(request, "request_body", None), "reply_in_thread", None) is True


@pytest.mark.asyncio
async def test_streaming_uses_create_for_initial_message_when_reply_disabled() -> None:
    channel, client = _build_channel(reply_to_message=False)

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="step-1",
            metadata={"_progress": True, "message_id": "src-create"},
        )
    )

    assert len(client.im.v1.message.create_calls) == 1
    assert len(client.im.v1.message.reply_calls) == 0


@pytest.mark.asyncio
async def test_streaming_rebinds_with_new_card_when_progress_cardkit_update_fails() -> None:
    channel, client = _build_channel(card_update_success=False, element_update_success=False)

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="step-1",
            metadata={"_progress": True, "message_id": "src-2"},
        )
    )
    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="step-2",
            metadata={"_progress": True, "message_id": "src-2"},
        )
    )

    assert len(client.cardkit.v1.card_element.content_calls) == 2
    assert len(client.cardkit.v1.card.update_calls) == 0
    assert len(client.im.v1.message.patch_calls) == 0
    assert _sent_message_count(client) == 2
    assert len(client.im.v1.message.delete_calls) == 1


@pytest.mark.asyncio
async def test_streaming_skips_single_card_mode_without_source_message_id() -> None:
    channel, client = _build_channel()

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="progress without source",
            metadata={"_progress": True},
        )
    )

    assert _sent_message_count(client) == 0
    assert not channel._stream_states


@pytest.mark.asyncio
async def test_non_progress_message_without_source_message_id_still_sends() -> None:
    channel, client = _build_channel()

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="normal command response",
            metadata={},
        )
    )

    assert _sent_message_count(client) == 1
    assert not channel._stream_states


@pytest.mark.asyncio
async def test_stream_state_ttl_cleanup_removes_stale_entries() -> None:
    channel, client = _build_channel(ttl_seconds=1)

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="step-1",
            metadata={"_progress": True, "message_id": "src-stale"},
        )
    )
    channel._stream_states["src-stale"].updated_at -= 10

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="step-1",
            metadata={"_progress": True, "message_id": "src-fresh"},
        )
    )

    assert "src-stale" not in channel._stream_states
    assert "src-fresh" in channel._stream_states
    assert _sent_message_count(client) == 2


@pytest.mark.asyncio
async def test_streaming_progress_updates_are_throttled() -> None:
    channel, client = _build_channel(min_update_ms=10_000)

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="step-1",
            metadata={"_progress": True, "message_id": "src-throttle"},
        )
    )
    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="step-2",
            metadata={"_progress": True, "message_id": "src-throttle"},
        )
    )

    assert _sent_message_count(client) == 1
    assert len(client.cardkit.v1.card_element.content_calls) == 0
    assert len(client.cardkit.v1.card.update_calls) == 0
    assert len(client.im.v1.message.update_calls) == 0


@pytest.mark.asyncio
async def test_thinking_updates_are_not_throttled() -> None:
    channel, client = _build_channel(min_update_ms=10_000)

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="准备调用 bitable_search",
            metadata={"_progress": True, "_progress_phase": "thinking", "message_id": "src-think-no-throttle"},
        )
    )
    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="调用 bitable_search，参数：{\"keyword\":\"x\"}",
            metadata={"_progress": True, "_progress_phase": "thinking", "message_id": "src-think-no-throttle"},
        )
    )

    assert _sent_message_count(client) == 1
    thinking_updates = [
        call for call in client.cardkit.v1.card_element.content_calls if getattr(call, "element_id", None) == "thinking_text"
    ]
    assert thinking_updates
    state = channel._stream_states["src-think-no-throttle"]
    assert "准备调用 bitable_search" in state.thinking_text
    assert "调用 bitable_search" in state.thinking_text


@pytest.mark.asyncio
async def test_streaming_skips_tiny_long_answer_progress_updates() -> None:
    channel, client = _build_channel(min_update_ms=0)
    first = "A" * 700
    second = "A" * 730
    third = "A" * 860

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content=first,
            metadata={"_progress": True, "_progress_phase": "answer", "message_id": "src-long-answer"},
        )
    )
    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content=second,
            metadata={"_progress": True, "_progress_phase": "answer", "message_id": "src-long-answer"},
        )
    )

    answer_updates = [
        call for call in client.cardkit.v1.card_element.content_calls if getattr(call, "element_id", None) == "answer_text"
    ]
    assert answer_updates == []

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content=third,
            metadata={"_progress": True, "_progress_phase": "answer", "message_id": "src-long-answer"},
        )
    )

    answer_updates = [
        call for call in client.cardkit.v1.card_element.content_calls if getattr(call, "element_id", None) == "answer_text"
    ]
    assert len(answer_updates) == 1
    assert channel._stream_states["src-long-answer"].answer_text == third


@pytest.mark.asyncio
async def test_streaming_final_fallback_creates_new_card_when_updates_fail() -> None:
    channel, client = _build_channel(
        card_update_success=False,
        element_update_success=False,
        update_handler=lambda request: _FakeResponse(False),
        patch_handler=lambda request: _FakeResponse(False),
    )

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="step-1",
            metadata={"_progress": True, "message_id": "src-final-fallback"},
        )
    )
    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="final",
            metadata={"message_id": "src-final-fallback"},
        )
    )

    assert _sent_message_count(client) == 2
    assert len(client.im.v1.message.delete_calls) == 1


@pytest.mark.asyncio
async def test_streaming_uses_patch_first_for_interactive_message_fallback() -> None:
    channel, client = _build_channel(card_update_success=False, element_update_success=False)

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="step-1",
            metadata={"_progress": True, "message_id": "src-put-retry"},
        )
    )
    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="final",
            metadata={"message_id": "src-put-retry"},
        )
    )

    assert _sent_message_count(client) == 1
    assert len(client.im.v1.message.patch_calls) == 1
    assert len(client.im.v1.message.update_calls) == 0


@pytest.mark.asyncio
async def test_streaming_rebinds_state_after_progress_fallback_send() -> None:
    def content_handler(request: Any) -> _FakeResponse:
        return _FakeResponse(request.card_id != "card-bot-1")

    channel, client = _build_channel(
        card_update_success=False,
        element_update_success=True,
        element_content_handler=content_handler,
    )

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="step-1",
            metadata={"_progress": True, "message_id": "src-rebind"},
        )
    )
    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="step-2",
            metadata={"_progress": True, "message_id": "src-rebind"},
        )
    )
    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="step-3",
            metadata={"_progress": True, "message_id": "src-rebind"},
        )
    )

    assert _sent_message_count(client) == 2
    assert any(call.card_id == "card-bot-2" for call in client.cardkit.v1.card_element.content_calls)


@pytest.mark.asyncio
async def test_tool_turn_final_response_updates_same_stream_card() -> None:
    channel, client = _build_channel()

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="正在查询中",
            metadata={"_progress": True, "message_id": "src-tool-final"},
        )
    )

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="查询结果：命中 2 条",
            metadata={"message_id": "src-tool-final", "_tool_turn": True},
        )
    )

    assert _sent_message_count(client) == 1
    assert "src-tool-final" in channel._stream_states
    assert any(call.element_id == "answer_text" for call in client.cardkit.v1.card_element.content_calls)


@pytest.mark.asyncio
async def test_thinking_done_keeps_thinking_detail_before_answer_stream() -> None:
    channel, client = _build_channel()

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="我来帮您查一下",
            metadata={"_progress": True, "_progress_phase": "thinking", "message_id": "src-clear"},
        )
    )

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="思考完成",
            metadata={"_progress": True, "_progress_phase": "thinking_done", "message_id": "src-clear"},
        )
    )

    assert "src-clear" in channel._stream_states
    assert len(client.cardkit.v1.card_element.content_calls) >= 1
    thinking_updates = [
        call for call in client.cardkit.v1.card_element.content_calls if getattr(call, "element_id", None) == "thinking_text"
    ]
    assert thinking_updates
    last_content = getattr(getattr(thinking_updates[-1], "request_body", None), "content", "")
    assert "我来帮您查一下" in last_content
    assert "已折叠" not in last_content


@pytest.mark.asyncio
async def test_thinking_progress_appends_details_in_same_card() -> None:
    channel, client = _build_channel()

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="先分析需求",
            metadata={"_progress": True, "_progress_phase": "thinking", "message_id": "src-think-append"},
        )
    )
    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="再检查可用工具",
            metadata={"_progress": True, "_progress_phase": "thinking", "message_id": "src-think-append"},
        )
    )

    state = channel._stream_states["src-think-append"]
    assert "先分析需求" in state.thinking_text
    assert "再检查可用工具" in state.thinking_text
    assert state.thinking_text.count("先分析需求") == 1

    thinking_updates = [
        call for call in client.cardkit.v1.card_element.content_calls if getattr(call, "element_id", None) == "thinking_text"
    ]
    assert thinking_updates
    merged = getattr(getattr(thinking_updates[-1], "request_body", None), "content", "")
    assert "先分析需求" in merged
    assert "再检查可用工具" in merged


def test_normalize_markdown_headings_for_feishu_output() -> None:
    content = "# 一级标题\n正文\n```md\n# 代码中的标题\n```\n## 二级标题"

    normalized = FeishuChannel._normalize_markdown_headings(content)

    assert "**一级标题**" in normalized
    assert "**二级标题**" in normalized
    assert "# 代码中的标题" in normalized


def test_streaming_card_payload_does_not_use_action_tag() -> None:
    channel, _ = _build_channel()

    payload = channel._build_streaming_initial_card_content("思考", "答案", False)
    card = json.loads(payload)
    tags = [str(el.get("tag")) for el in card.get("body", {}).get("elements", [])]

    assert "action" not in tags


def test_thinking_block_uses_quoted_subtle_style() -> None:
    channel, _ = _build_channel()

    expanded = channel._format_thinking_block("检索中", collapsed=False)
    collapsed = channel._format_thinking_block("检索中", collapsed=True)

    assert expanded.startswith("> ")
    assert collapsed.startswith("> ")
    assert "已折叠" not in collapsed
    assert "检索中" in collapsed


def test_thinking_block_hides_generic_placeholder_when_specific_detail_exists() -> None:
    channel, _ = _build_channel()

    block = channel._format_thinking_block("正在思考中...\n调用 bitable_search，参数：{\"q\":\"abc\"}", collapsed=False)

    assert "正在思考中..." not in block
    assert "调用 bitable_search" in block


def test_thinking_block_uses_placeholders_when_only_generic_lines_exist() -> None:
    channel, _ = _build_channel()

    expanded = channel._format_thinking_block("思考中\n思考完成", collapsed=False)
    collapsed = channel._format_thinking_block("思考中\n思考完成", collapsed=True)

    assert "思考中" in expanded
    assert "思考完成" in collapsed


def test_streaming_initial_answer_card_hides_thinking_placeholder_when_no_details() -> None:
    channel, _ = _build_channel(show_thinking=True)

    payload = channel._build_streaming_initial_card_content("", "最终回复", True)
    card = json.loads(payload)
    elements = card["body"]["elements"]
    thinking = elements[0]["content"]

    assert thinking == ""
    assert not any(element.get("content") == "---" for element in elements)


@pytest.mark.asyncio
async def test_streaming_ignores_generic_thinking_placeholder_before_answer() -> None:
    channel, client = _build_channel()

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="思考中",
            metadata={"_progress": True, "_progress_phase": "thinking", "message_id": "src-generic-think"},
        )
    )

    assert _sent_message_count(client) == 0
    assert "src-generic-think" not in channel._stream_states

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="最终答案",
            metadata={"message_id": "src-generic-think"},
        )
    )

    assert _sent_message_count(client) == 1
    assert "src-generic-think" not in channel._stream_states
    assert all(getattr(call, "element_id", None) != "thinking_text" for call in client.cardkit.v1.card_element.content_calls)


@pytest.mark.asyncio
async def test_streaming_first_real_answer_bypasses_throttle_after_placeholder() -> None:
    channel, client = _build_channel(min_update_ms=10_000)

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="🐈努力回答中...",
            metadata={"_progress": True, "_progress_phase": "answer", "message_id": "src-placeholder-throttle"},
        )
    )
    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="这里是正式答案",
            metadata={"_progress": True, "_progress_phase": "answer", "message_id": "src-placeholder-throttle"},
        )
    )

    answer_updates = [
        call for call in client.cardkit.v1.card_element.content_calls if getattr(call, "element_id", None) == "answer_text"
    ]
    assert answer_updates
    assert channel._stream_states["src-placeholder-throttle"].answer_text == "这里是正式答案"


@pytest.mark.asyncio
async def test_streaming_can_hide_thinking_section_and_progress_phases() -> None:
    channel, client = _build_channel(show_thinking=False)

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="正在思考中...",
            metadata={"_progress": True, "_progress_phase": "thinking", "message_id": "src-hide-think"},
        )
    )
    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="思考完成",
            metadata={"_progress": True, "_progress_phase": "thinking_done", "message_id": "src-hide-think"},
        )
    )

    assert _sent_message_count(client) == 0
    assert "src-hide-think" not in channel._stream_states

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="答案内容",
            metadata={"_progress": True, "_progress_phase": "answer", "message_id": "src-hide-think"},
        )
    )

    assert _sent_message_count(client) == 1
    card = _first_sent_card(client)
    elements = card.get("body", {}).get("elements", [])
    assert len(elements) == 1
    assert elements[0].get("element_id") == "answer_text"

    state = channel._stream_states["src-hide-think"]
    assert state.thinking_text == ""
    assert state.thinking_collapsed is True

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="最终答案",
            metadata={"message_id": "src-hide-think"},
        )
    )

    assert all(getattr(call, "element_id", None) != "thinking_text" for call in client.cardkit.v1.card_element.content_calls)


@pytest.mark.asyncio
async def test_on_message_skips_missing_optional_thread_fields() -> None:
    channel, _ = _build_channel()
    channel.config.activation_group_policy = "always"
    captured: dict[str, Any] = {}

    async def _fake_handle_message(**kwargs: Any) -> None:
        captured.update(kwargs)

    channel._handle_message = _fake_handle_message  # type: ignore[assignment]

    data = SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id="msg-no-threads",
                chat_id="oc_group_1",
                chat_type="group",
                message_type="text",
                content=json.dumps({"text": "hello"}, ensure_ascii=False),
            ),
            sender=SimpleNamespace(
                sender_type="user",
                sender_id=SimpleNamespace(open_id="ou_sender_1"),
            ),
        )
    )

    await channel._on_message(data)  # type: ignore[arg-type]

    assert captured["sender_id"] == "ou_sender_1"
    assert captured["chat_id"] == "oc_group_1"
    assert captured["content"] == "hello"
    assert captured["metadata"]["message_id"] == "msg-no-threads"
    assert "upper_message_id" not in captured["metadata"]
    assert captured.get("session_key") is None


@pytest.mark.asyncio
async def test_send_uses_custom_interactive_content_when_provided() -> None:
    channel, client = _build_channel()
    custom_card = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "markdown", "content": "# custom"},
        ],
    }

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="fallback text",
            metadata={"interactive_content": json.dumps(custom_card, ensure_ascii=False)},
        )
    )

    assert _sent_message_count(client) == 1
    sent = _first_sent_card(client)
    assert sent == custom_card


@pytest.mark.asyncio
async def test_send_updates_existing_message_when_update_message_id_is_set() -> None:
    channel, client = _build_channel()
    channel.config.stream_card_enabled = False
    custom_card = {
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "markdown", "content": "# updated"}],
    }

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="ignored fallback",
            metadata={
                "message_id": "src-should-not-reply",
                "interactive_content": json.dumps(custom_card, ensure_ascii=False),
                "_update_message_id": "om_to_update_1",
                "_disable_reply_to_message": True,
            },
        )
    )

    assert len(client.im.v1.message.update_calls) + len(client.im.v1.message.patch_calls) == 1
    assert len(client.im.v1.message.reply_calls) == 0
    assert len(client.im.v1.message.create_calls) == 0


def test_streaming_body_keeps_thinking_element_even_when_empty() -> None:
    channel, _ = _build_channel(show_thinking=True)

    elements = channel._build_streaming_body_elements("", "最终回复")

    assert len(elements) == 2
    assert elements[0]["element_id"] == "thinking_text"
    assert elements[0]["content"] == ""
    assert elements[1]["element_id"] == "answer_text"


@pytest.mark.asyncio
async def test_on_message_routes_group_thread_to_independent_session_key() -> None:
    channel, _ = _build_channel()
    captured: dict[str, Any] = {}

    async def _fake_handle_message(**kwargs: Any) -> None:
        captured.update(kwargs)

    channel._handle_message = _fake_handle_message  # type: ignore[assignment]

    data = SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id="msg-thread-1",
                chat_id="oc_group_1",
                chat_type="group",
                message_type="text",
                content=json.dumps({"text": "thread hello"}, ensure_ascii=False),
                thread_id="thread_x",
            ),
            sender=SimpleNamespace(
                sender_type="user",
                sender_id=SimpleNamespace(open_id="ou_sender_1"),
            ),
        )
    )

    await channel._on_message(data)  # type: ignore[arg-type]

    assert captured["chat_id"] == "oc_group_1"
    assert captured["session_key"] == "feishu:oc_group_1:thread_x"
