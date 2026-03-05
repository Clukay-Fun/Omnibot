import json

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.skill_runtime.user_memory import UserMemoryStore
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ChannelsConfig, FeishuConfig, SkillSpecConfig
from nanobot.providers.base import LLMResponse


class _DummyProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        return LLMResponse(content="llm-fallback", tool_calls=[])

    def get_default_model(self) -> str:
        return "dummy"


def _build_loop(tmp_path):
    provider = _DummyProvider()
    channels = ChannelsConfig(
        feishu=FeishuConfig(
            onboarding_enabled=True,
            onboarding_reentry_commands=["/setup", "重新设置"],
        )
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        channels_config=channels,
        skillspec_config=SkillSpecConfig(enabled=True),
    )
    return loop, provider


@pytest.mark.asyncio
async def test_first_feishu_message_triggers_onboarding_identity_card(tmp_path) -> None:
    loop, provider = _build_loop(tmp_path)

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_new",
            chat_id="oc_group",
            content="你好",
            metadata={"chat_type": "group", "message_id": "m-1"},
        )
    )

    assert response is not None
    assert response.metadata.get("onboarding_stage") == "identity"
    assert "interactive_content" in response.metadata
    card = json.loads(response.metadata["interactive_content"])
    assert card["header"]["title"]["content"] == "欢迎使用 Omnibot"
    assert provider.calls == 0

    store = UserMemoryStore(tmp_path)
    profile = store.read("feishu", "ou_new")
    assert profile["onboarding"]["status"] == "pending"
    assert profile["onboarding"]["step"] == "identity"


@pytest.mark.asyncio
async def test_onboarding_card_actions_progress_and_complete(tmp_path) -> None:
    loop, provider = _build_loop(tmp_path)
    store = UserMemoryStore(tmp_path)

    await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_new",
            chat_id="oc_group",
            content="你好",
            metadata={"chat_type": "group", "message_id": "m-1"},
        )
    )

    identity_submit = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_new",
            chat_id="oc_group",
            content='[feishu card action trigger]\naction_key: onboarding_identity_submit\nform_value: {"display_name":"张三","role":"律师","team":"诉讼组"}',
            metadata={
                "msg_type": "card_action",
                "action_key": "onboarding_identity_submit",
                "chat_type": "group",
                "message_id": "om-1",
            },
        )
    )

    assert identity_submit is not None
    assert identity_submit.metadata.get("onboarding_stage") == "preference"
    assert identity_submit.metadata.get("_update_message_id") == "om-1"
    assert identity_submit.metadata.get("_disable_reply_to_message") is True
    profile_after_identity = store.read("feishu", "ou_new")
    assert profile_after_identity["identity"]["name"] == "张三"
    assert profile_after_identity["identity"]["role"] == "律师"
    assert profile_after_identity["onboarding"]["step"] == "preference"

    pref_submit = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_new",
            chat_id="oc_group",
            content='[feishu card action trigger]\naction_key: onboarding_pref_submit\nform_value: {"response_style":"concise","write_confirm":"auto","preferred_name":"张律"}',
            metadata={
                "msg_type": "card_action",
                "action_key": "onboarding_pref_submit",
                "chat_type": "group",
                "message_id": "om-2",
            },
        )
    )

    assert pref_submit is not None
    assert "初始化完成" in pref_submit.content
    assert pref_submit.metadata.get("onboarding_stage") == "completed"
    assert pref_submit.metadata.get("_update_message_id") == "om-2"
    assert pref_submit.metadata.get("_disable_reply_to_message") is True
    profile_after_pref = store.read("feishu", "ou_new")
    assert profile_after_pref["onboarding"]["status"] == "completed"
    assert profile_after_pref["skillspec"]["confirm_preference"] == "auto"
    assert profile_after_pref["preferences"]["preferred_name"] == "张律"

    normal_reply = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_new",
            chat_id="oc_group",
            content="现在查一下任务",
            metadata={"chat_type": "group", "message_id": "m-2"},
        )
    )
    assert normal_reply is not None
    assert normal_reply.content == "llm-fallback"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_setup_command_restarts_onboarding_after_completion(tmp_path) -> None:
    loop, _ = _build_loop(tmp_path)
    store = UserMemoryStore(tmp_path)
    store.write(
        "feishu",
        "ou_existing",
        {
            "identity": {"name": "李四"},
            "preferences": {},
            "dynamic": {},
            "skillspec": {},
            "onboarding": {"status": "completed", "step": "completed"},
        },
    )

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_existing",
            chat_id="oc_group",
            content="/setup",
            metadata={"chat_type": "group", "message_id": "m-setup"},
        )
    )

    assert response is not None
    assert response.metadata.get("onboarding_stage") == "identity"
    profile = store.read("feishu", "ou_existing")
    assert profile["onboarding"]["status"] == "pending"
    assert profile["onboarding"]["step"] == "identity"


@pytest.mark.asyncio
async def test_completed_onboarding_card_action_returns_completed_card_and_no_llm(tmp_path) -> None:
    loop, provider = _build_loop(tmp_path)
    store = UserMemoryStore(tmp_path)
    store.write(
        "feishu",
        "ou_done",
        {
            "identity": {"name": "已完成用户"},
            "preferences": {},
            "dynamic": {},
            "skillspec": {},
            "onboarding": {"status": "completed", "step": "completed"},
        },
    )

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_done",
            chat_id="oc_group",
            content='[feishu card action trigger]\naction_key: onboarding_pref_submit\nform_value: {"response_style":"concise"}',
            metadata={
                "msg_type": "card_action",
                "action_key": "onboarding_pref_submit",
                "chat_type": "group",
                "message_id": "om-done-1",
            },
        )
    )

    assert response is not None
    assert response.metadata.get("onboarding_stage") == "completed"
    assert response.metadata.get("_update_message_id") == "om-done-1"
    assert "初始化已完成" in response.content
    assert provider.calls == 0
