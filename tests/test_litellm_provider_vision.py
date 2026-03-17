from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.providers.litellm_provider import LiteLLMProvider


@pytest.mark.asyncio
async def test_chat_filters_image_url_for_non_vision_model(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_completion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok"),
                    finish_reason="stop",
                )
            ]
        )

    monkeypatch.setattr("nanobot.providers.litellm_provider.acompletion", _fake_completion)

    provider = LiteLLMProvider(default_model="fake/non-vision")
    monkeypatch.setattr(provider, "_supports_vision", lambda _model: False)

    await provider.chat(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            }
        ],
        model="fake/non-vision",
    )

    sent_messages = captured["messages"]
    assert sent_messages[0]["content"][1] == {"type": "text", "text": "[image]"}


def test_filter_image_url_preserves_non_image_blocks():
    provider = LiteLLMProvider(default_model="fake/non-vision")
    filtered = provider._filter_image_url(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            }
        ]
    )
    assert filtered[0]["content"][0] == {"type": "text", "text": "hello"}
    assert filtered[0]["content"][1] == {"type": "text", "text": "[image]"}
