import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse


class _DummyProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, **kwargs):  # noqa: ARG002
        self.calls += 1
        return LLMResponse(content=f"llm-fallback-{self.calls}", tool_calls=[])

    def get_default_model(self) -> str:
        return "dummy"


@pytest.mark.asyncio
async def test_loop_treats_legacy_skill_command_as_normal_prompt(tmp_path) -> None:
    provider = _DummyProvider()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="u1", chat_id="chat", content="/skill query_test 关键词")
    )

    assert response is not None
    assert response.content == "llm-fallback-1"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_loop_treats_legacy_continuation_prompt_as_normal_prompt(tmp_path) -> None:
    provider = _DummyProvider()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)

    response = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="oc_group", content="继续")
    )

    assert response is not None
    assert response.content == "llm-fallback-1"
    assert provider.calls == 1
