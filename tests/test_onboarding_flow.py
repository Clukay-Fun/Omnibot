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
async def test_first_feishu_message_triggers_single_onboarding_card(tmp_path) -> None:
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
    assert response.metadata.get("onboarding_stage") == "single"
    assert "interactive_content" in response.metadata
    card = json.loads(response.metadata["interactive_content"])
    assert card["header"]["title"]["content"] == "👋 你好，我是 Omnibot"
    assert card["config"]["update_multi"] is True
    assert card["elements"][2]["tag"] == "form"
    assert card["elements"][2]["name"] == "onboarding_form"
    assert provider.calls == 0

    store = UserMemoryStore(tmp_path)
    profile = store.read("feishu", "ou_new")
    assert profile["onboarding"]["status"] == "pending"
    assert profile["onboarding"]["step"] == "identity"


@pytest.mark.asyncio
async def test_first_feishu_slash_command_also_triggers_single_onboarding_card(tmp_path) -> None:
    loop, provider = _build_loop(tmp_path)

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_new_slash",
            chat_id="oc_group",
            content="/help",
            metadata={"chat_type": "group", "message_id": "m-1"},
        )
    )

    assert response is not None
    assert response.metadata.get("onboarding_stage") == "single"
    assert "interactive_content" in response.metadata
    assert "可用指令" not in response.content
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_single_submit_completes_onboarding_and_writes_all_fields(tmp_path) -> None:
    loop, provider = _build_loop(tmp_path)
    store = UserMemoryStore(tmp_path)

    await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_submit",
            chat_id="oc_group",
            content="你好",
            metadata={"chat_type": "group", "message_id": "m-1"},
        )
    )

    submit = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_submit",
            chat_id="oc_group",
            content='[feishu card action trigger]\naction_name: submit_onboarding\naction_key: submit_onboarding\nform_value: {"user_name":"张三","role":"lawyer","team":"litigation_1","tone":"concise","confirm_write":"no","query_scope":"all","display_name":"张律"}',
            metadata={
                "msg_type": "card_action",
                "action_name": "submit_onboarding",
                "action_key": "submit_onboarding",
                "chat_type": "group",
                "message_id": "om-1",
            },
        )
    )

    assert submit is not None
    assert submit.metadata.get("onboarding_stage") == "completed"
    assert submit.metadata.get("_update_message_id") == "om-1"
    assert submit.metadata.get("_disable_reply_to_message") is True
    profile = store.read("feishu", "ou_submit")
    assert profile["identity"]["name"] == "张三"
    assert profile["identity"]["role"] == "lawyer"
    assert profile["identity"]["team"] == "litigation_1"
    assert profile["preferences"]["response_style"] == "concise"
    assert profile["preferences"]["preferred_name"] == "张律"
    assert profile["preferences"]["query_scope"] == "all"
    assert profile["skillspec"]["confirm_preference"] == "auto"
    assert profile["onboarding"]["status"] == "completed"
    assert profile["onboarding"]["step"] == "completed"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_skip_directly_completes_onboarding_with_defaults(tmp_path) -> None:
    loop, provider = _build_loop(tmp_path)
    store = UserMemoryStore(tmp_path)

    await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_skip",
            chat_id="oc_group",
            content="你好",
            metadata={"chat_type": "group", "message_id": "m-1"},
        )
    )

    skip = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_skip",
            chat_id="oc_group",
            content="[feishu card action trigger]\naction_name: skip_onboarding\naction_key: skip_onboarding",
            metadata={
                "msg_type": "card_action",
                "action_name": "skip_onboarding",
                "action_key": "skip_onboarding",
                "chat_type": "group",
                "message_id": "om-2",
            },
        )
    )

    assert skip is not None
    assert skip.metadata.get("onboarding_stage") == "completed"
    assert skip.metadata.get("_update_message_id") == "om-2"
    profile = store.read("feishu", "ou_skip")
    assert profile["onboarding"]["status"] == "completed"
    assert profile["preferences"]["response_style"] == "standard"
    assert profile["preferences"]["query_scope"] == "self"
    assert profile["skillspec"]["confirm_preference"] == "manual"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_completed_onboarding_card_click_is_idempotent(tmp_path) -> None:
    loop, provider = _build_loop(tmp_path)
    store = UserMemoryStore(tmp_path)

    await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_done",
            chat_id="oc_group",
            content="你好",
            metadata={"chat_type": "group", "message_id": "m-1"},
        )
    )

    await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_done",
            chat_id="oc_group",
            content='[feishu card action trigger]\naction_name: submit_onboarding\naction_key: submit_onboarding\nform_value: {"user_name":"首次","tone":"detailed","confirm_write":"no"}',
            metadata={
                "msg_type": "card_action",
                "action_name": "submit_onboarding",
                "action_key": "submit_onboarding",
                "chat_type": "group",
                "message_id": "om-done-1",
            },
        )
    )

    profile_before_repeat = store.read("feishu", "ou_done")

    repeat_click = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_done",
            chat_id="oc_group",
            content='[feishu card action trigger]\naction_key: onboarding_pref_submit\nform_value: {"display_name":"二次","response_style":"concise"}',
            metadata={
                "msg_type": "card_action",
                "action_key": "onboarding_pref_submit",
                "chat_type": "group",
                "message_id": "om-done-2",
            },
        )
    )

    assert repeat_click is not None
    assert repeat_click.metadata.get("onboarding_stage") == "completed"
    assert repeat_click.metadata.get("_update_message_id") == "om-done-2"
    profile_after_repeat = store.read("feishu", "ou_done")
    assert profile_after_repeat == profile_before_repeat
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_completed_user_greeting_uses_llm_instead_of_fixed_reply(tmp_path) -> None:
    loop, provider = _build_loop(tmp_path)
    store = UserMemoryStore(tmp_path)
    store.write(
        "feishu",
        "ou_greeting",
        {
            "identity": {"name": "张三"},
            "preferences": {"preferred_name": "张律"},
            "dynamic": {},
            "skillspec": {"confirm_preference": "manual"},
            "onboarding": {"status": "completed", "step": "completed"},
        },
    )

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_greeting",
            chat_id="oc_group",
            content="您好",
            metadata={"chat_type": "group", "message_id": "m-intro-2"},
        )
    )

    assert response is not None
    assert response.content == "llm-fallback"
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
    assert response.metadata.get("onboarding_stage") == "single"
    profile = store.read("feishu", "ou_existing")
    assert profile["onboarding"]["status"] == "pending"
    assert profile["onboarding"]["step"] == "identity"


@pytest.mark.asyncio
async def test_status_command_returns_current_preferences(tmp_path) -> None:
    loop, provider = _build_loop(tmp_path)
    store = UserMemoryStore(tmp_path)
    store.write(
        "feishu",
        "ou_status",
        {
            "identity": {"name": "张三", "role": "lawyer"},
            "preferences": {
                "response_style": "concise",
                "preferred_name": "张律",
                "query_scope": "all",
            },
            "dynamic": {},
            "skillspec": {"confirm_preference": "auto"},
            "onboarding": {"status": "completed", "step": "completed"},
        },
    )

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_status",
            chat_id="oc_group",
            content="/status",
            metadata={"chat_type": "group", "message_id": "m-status"},
        )
    )

    assert response is not None
    assert "📌 当前设置" in response.content
    assert "怎么称呼您：张律" in response.content
    assert "回复风格：简洁" in response.content
    assert "录入数据时：直接写入，不用每次确认" in response.content
    assert "查案件时默认范围：查全部" in response.content
    assert provider.calls == 0
