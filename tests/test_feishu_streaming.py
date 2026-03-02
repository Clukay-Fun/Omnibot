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
        self.update_calls: list[object] = []
        self.patch_calls: list[object] = []
        self._update_handler = update_handler
        self._patch_handler = patch_handler

    def create(self, request: object) -> _FakeResponse:
        self.create_calls.append(request)
        message_id = f"bot-{len(self.create_calls)}"
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


def _build_channel(
    *,
    card_update_success: bool = True,
    ttl_seconds: int = 600,
    min_update_ms: int = 0,
    update_handler: Callable[[Any], _FakeResponse] | None = None,
    patch_handler: Callable[[Any], _FakeResponse] | None = None,
) -> tuple[FeishuChannel, SimpleNamespace]:
    config = FeishuConfig(
        stream_card_enabled=True,
        stream_card_use_cardkit=True,
        stream_card_min_update_ms=min_update_ms,
        stream_card_ttl_seconds=ttl_seconds,
    )
    channel = FeishuChannel(config=config, bus=MessageBus())
    message_api = _FakeMessageAPI(update_handler=update_handler, patch_handler=patch_handler)
    card_api = _FakeCardAPI(update_success=card_update_success)
    client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=message_api)),
        cardkit=SimpleNamespace(v1=SimpleNamespace(card=card_api)),
    )
    channel._client = client
    return channel, client


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

    assert len(client.im.v1.message.create_calls) == 1
    assert len(client.cardkit.v1.card.id_convert_calls) == 1
    assert len(client.cardkit.v1.card.settings_calls) == 1
    assert len(client.cardkit.v1.card.update_calls) == 2
    assert len(client.im.v1.message.update_calls) == 0
    assert "src-1" not in channel._stream_states


@pytest.mark.asyncio
async def test_streaming_falls_back_to_im_update_when_cardkit_update_fails() -> None:
    channel, client = _build_channel(card_update_success=False)

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

    assert len(client.cardkit.v1.card.update_calls) == 1
    assert len(client.im.v1.message.update_calls) == 1


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

    assert len(client.im.v1.message.create_calls) == 1
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
    assert len(client.im.v1.message.create_calls) == 2


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

    assert len(client.im.v1.message.create_calls) == 1
    assert len(client.cardkit.v1.card.update_calls) == 0
    assert len(client.im.v1.message.update_calls) == 0


@pytest.mark.asyncio
async def test_streaming_final_fallback_creates_new_card_when_updates_fail() -> None:
    channel, client = _build_channel(
        card_update_success=False,
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

    assert len(client.im.v1.message.create_calls) == 2


@pytest.mark.asyncio
async def test_streaming_retries_put_without_msg_type_before_patch() -> None:
    def update_handler(request: Any) -> _FakeResponse:
        msg_type = getattr(getattr(request, "request_body", None), "msg_type", None)
        return _FakeResponse(msg_type is None)

    channel, client = _build_channel(card_update_success=False, update_handler=update_handler)

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
            content="step-2",
            metadata={"_progress": True, "message_id": "src-put-retry"},
        )
    )

    assert len(client.im.v1.message.create_calls) == 1
    assert len(client.im.v1.message.update_calls) == 2
    assert len(client.im.v1.message.patch_calls) == 0


@pytest.mark.asyncio
async def test_streaming_rebinds_state_after_progress_fallback_send() -> None:
    def update_handler(request: Any) -> _FakeResponse:
        return _FakeResponse(request.message_id != "bot-1")

    def patch_handler(request: Any) -> _FakeResponse:
        return _FakeResponse(request.message_id != "bot-1")

    channel, client = _build_channel(
        card_update_success=False,
        update_handler=update_handler,
        patch_handler=patch_handler,
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

    assert len(client.im.v1.message.create_calls) == 2
    assert any(call.message_id == "bot-2" for call in client.im.v1.message.update_calls)
