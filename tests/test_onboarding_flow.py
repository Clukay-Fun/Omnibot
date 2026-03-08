import asyncio
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
        self.last_messages = []
        self.last_kwargs = {}

    async def chat(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        self.last_messages = list(kwargs.get("messages") or [])
        return LLMResponse(content="llm-fallback", tool_calls=[])

    def get_default_model(self) -> str:
        return "dummy"


def _build_loop(tmp_path):
    provider = _DummyProvider()
    channels = ChannelsConfig(
        feishu=FeishuConfig(
            onboarding_enabled=True,
            onboarding_blocking=True,
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
async def test_non_blocking_onboarding_prompts_once_without_blocking_dialogue(tmp_path) -> None:
    provider = _DummyProvider()
    bus = MessageBus()
    channels = ChannelsConfig(
        feishu=FeishuConfig(
            onboarding_enabled=True,
            onboarding_blocking=False,
            onboarding_guide_once=True,
            onboarding_reentry_commands=["/setup", "重新设置"],
        )
    )
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        channels_config=channels,
        skillspec_config=SkillSpecConfig(enabled=True),
    )

    first = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_non_blocking",
            chat_id="oc_group",
            content="你好",
            metadata={"chat_type": "group", "message_id": "m-1"},
        )
    )

    assert first is not None
    assert first.content == "llm-fallback"
    assert provider.calls == 1

    first_outbounds = []
    while bus.outbound_size:
        first_outbounds.append(await asyncio.wait_for(bus.consume_outbound(), timeout=1))

    guide_messages = [item for item in first_outbounds if item.metadata.get("onboarding") is True]
    assert len(guide_messages) == 1
    assert guide_messages[0].metadata.get("onboarding_stage") == "guide"
    assert "BOOTSTRAP.md" in guide_messages[0].content
    assert "按默认继续" in guide_messages[0].content
    assert "快速上手" not in guide_messages[0].content

    store = UserMemoryStore(tmp_path)
    profile = store.read("feishu", "ou_non_blocking")
    assert profile["onboarding"]["status"] == "completed"
    assert profile["onboarding"]["step"] == "bootstrap_default"

    second = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_non_blocking",
            chat_id="oc_group",
            content="你是谁",
            metadata={"chat_type": "group", "message_id": "m-2"},
        )
    )

    assert second is not None
    assert second.content == "llm-fallback"
    assert provider.calls == 2

    second_outbounds = []
    while bus.outbound_size:
        second_outbounds.append(await asyncio.wait_for(bus.consume_outbound(), timeout=1))

    assert not any(item.metadata.get("onboarding") is True for item in second_outbounds)


@pytest.mark.asyncio
async def test_setup_reentry_uses_bootstrap_confirmation_text(tmp_path) -> None:
    provider = _DummyProvider()
    bus = MessageBus()
    channels = ChannelsConfig(
        feishu=FeishuConfig(
            onboarding_enabled=True,
            onboarding_blocking=False,
            onboarding_guide_once=True,
            onboarding_reentry_commands=["/setup", "重新设置"],
        )
    )
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        channels_config=channels,
        skillspec_config=SkillSpecConfig(enabled=True),
    )

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_reentry",
            chat_id="oc_group",
            content="/setup",
            metadata={"chat_type": "group", "message_id": "m-1"},
        )
    )

    assert response is not None
    guide = response
    assert guide.metadata.get("onboarding_stage") == "guide_reentry"
    assert "BOOTSTRAP.md" in guide.content
    assert "按默认继续" in guide.content


@pytest.mark.asyncio
async def test_private_first_message_uses_bootstrap_llm_as_normal_reply(tmp_path) -> None:
    provider = _DummyProvider()
    bus = MessageBus()
    (tmp_path / "BOOTSTRAP.md").write_text("1. **Your name**\n2. **Your vibe**", encoding="utf-8")
    channels = ChannelsConfig(
        feishu=FeishuConfig(
            onboarding_enabled=True,
            onboarding_blocking=False,
            onboarding_guide_once=True,
            onboarding_reentry_commands=["/setup", "重新设置"],
        )
    )
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        channels_config=channels,
        skillspec_config=SkillSpecConfig(enabled=True),
    )

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_private_first",
            chat_id="ou_private_first",
            content="你好",
            metadata={"chat_type": "p2p", "message_id": "m-1"},
        )
    )

    assert response is not None
    assert response.content == "llm-fallback"
    assert provider.calls == 1
    assert response.metadata.get("onboarding") is None
    queued = []
    while bus.outbound_size:
        queued.append(await asyncio.wait_for(bus.consume_outbound(), timeout=1))
    assert not any(item.metadata.get("onboarding") is True for item in queued)
    system_prompt = "\n".join(str(msg.get("content")) for msg in provider.last_messages if msg.get("role") == "system")
    assert "## BOOTSTRAP.md" in system_prompt
    assert "Do not skip the bootstrap conversation" in system_prompt
    assert str(provider.last_messages[-1]["content"]).startswith("[Bootstrap Internal Trigger]")
    assert "Actual user message:\n你好" in str(provider.last_messages[-1]["content"])

    store = UserMemoryStore(tmp_path)
    profile = store.read("feishu", "ou_private_first")
    assert profile["onboarding"]["status"] == "completed"
    assert profile["onboarding"]["step"] == "bootstrap_default"


@pytest.mark.asyncio
async def test_private_setup_reentry_is_model_driven_not_fixed_guide(tmp_path) -> None:
    provider = _DummyProvider()
    bus = MessageBus()
    user_dir = tmp_path / "memory" / "feishu" / "users" / "ou_private_reentry"
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "BOOTSTRAP.md").write_text("1. **Your name**\n2. **Your nature**", encoding="utf-8")
    channels = ChannelsConfig(
        feishu=FeishuConfig(
            onboarding_enabled=True,
            onboarding_blocking=False,
            onboarding_guide_once=True,
            onboarding_reentry_commands=["/setup", "重新设置"],
        )
    )
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        channels_config=channels,
        skillspec_config=SkillSpecConfig(enabled=True),
    )

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_private_reentry",
            chat_id="ou_private_reentry",
            content="/setup",
            metadata={"chat_type": "p2p", "message_id": "m-setup"},
        )
    )

    assert response is not None
    assert response.content == "llm-fallback"
    assert response.metadata.get("onboarding") is None
    assert provider.calls == 1
    assert any(msg.get("role") == "system" and "## BOOTSTRAP.md" in str(msg.get("content")) for msg in provider.last_messages)
    assert str(provider.last_messages[-1]["content"]).startswith("[Bootstrap Internal Trigger]")


@pytest.mark.asyncio
async def test_private_p2p_chat_create_bootstrap_is_model_driven(tmp_path) -> None:
    provider = _DummyProvider()
    bus = MessageBus()
    channels = ChannelsConfig(
        feishu=FeishuConfig(
            onboarding_enabled=True,
            onboarding_blocking=False,
            onboarding_guide_once=True,
            onboarding_reentry_commands=["/setup", "重新设置"],
        )
    )
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        channels_config=channels,
        skillspec_config=SkillSpecConfig(enabled=True),
    )

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_p2p_create",
            chat_id="ou_p2p_create",
            content="",
            metadata={
                "chat_type": "p2p",
                "source_event_type": "p2p_chat_create",
                "_bootstrap": True,
                "_bootstrap_proactive": True,
            },
        )
    )

    assert response is not None
    assert response.content == "llm-fallback"
    assert provider.calls == 1
    assert response.metadata.get("_disable_reply_to_message") is True
    assert str(provider.last_messages[-1]["content"]).startswith("[Bootstrap Internal Trigger]")


@pytest.mark.asyncio
async def test_private_setup_reentry_prefers_user_persona_files(tmp_path) -> None:
    provider = _DummyProvider()
    bus = MessageBus()
    (tmp_path / "BOOTSTRAP.md").write_text("1. **Shared vibe**", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("- shared-soul-line", encoding="utf-8")
    user_dir = tmp_path / "memory" / "feishu" / "users" / "ou_private"
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "BOOTSTRAP.md").write_text(
        """
# BOOTSTRAP.md - Hello, World

Start with something like:

> "Hey. I just came online. Who am I? Who are you?"

Then figure out together:

1. **Your name** — What should they call you?
2. **Your nature** — What kind of creature are you?

Update these files with what you learned:

- `IDENTITY.md` — your name, creature, vibe, emoji
- `USER.md` — their name, how to address them, timezone, notes
""".strip(),
        encoding="utf-8",
    )
    (user_dir / "SOUL.md").write_text(
        """
## Response Defaults

- Default to concise answers unless detail is clearly useful.

## Boundaries

- Ask before external actions or public output.
""".strip(),
        encoding="utf-8",
    )

    channels = ChannelsConfig(
        feishu=FeishuConfig(
            onboarding_enabled=True,
            onboarding_blocking=False,
            onboarding_guide_once=True,
            onboarding_reentry_commands=["/setup", "重新设置"],
        )
    )
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        channels_config=channels,
        skillspec_config=SkillSpecConfig(enabled=True),
    )

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_private",
            chat_id="ou_private",
            content="/setup",
            metadata={"chat_type": "p2p", "message_id": "m-private-setup"},
        )
    )

    assert response is not None
    assert response.content == "llm-fallback"
    system_prompt = "\n".join(str(msg.get("content")) for msg in provider.last_messages if msg.get("role") == "system")
    assert "Hey. I just came online. Who am I? Who are you?" in system_prompt
    assert "Default to concise answers unless detail is clearly useful." in system_prompt
    assert "Ask before external actions or public output." in system_prompt


@pytest.mark.asyncio
async def test_private_setup_seeds_user_persona_files_from_workspace_defaults(tmp_path) -> None:
    provider = _DummyProvider()
    bus = MessageBus()
    for name, content in {
        "BOOTSTRAP.md": "shared-bootstrap",
        "SOUL.md": "shared-soul",
        "USER.md": "shared-user",
        "IDENTITY.md": "shared-identity",
        "MEMORY.md": "shared-memory",
    }.items():
        (tmp_path / name).write_text(content, encoding="utf-8")

    channels = ChannelsConfig(
        feishu=FeishuConfig(
            onboarding_enabled=True,
            onboarding_blocking=False,
            onboarding_guide_once=True,
            onboarding_reentry_commands=["/setup", "重新设置"],
        )
    )
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        channels_config=channels,
        skillspec_config=SkillSpecConfig(enabled=True),
    )

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_seed",
            chat_id="ou_seed",
            content="/setup",
            metadata={"chat_type": "p2p", "message_id": "m-seed"},
        )
    )

    assert response is not None
    user_dir = tmp_path / "memory" / "feishu" / "users" / "ou_seed"
    assert (user_dir / "BOOTSTRAP.md").read_text(encoding="utf-8") == "shared-bootstrap"
    assert (user_dir / "SOUL.md").read_text(encoding="utf-8") == "shared-soul"
    assert (user_dir / "USER.md").read_text(encoding="utf-8") == "shared-user"
    assert (user_dir / "IDENTITY.md").read_text(encoding="utf-8") == "shared-identity"
    assert (user_dir / "MEMORY.md").read_text(encoding="utf-8") == "shared-memory"


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
async def test_skip_directly_completes_onboarding_without_forced_preferences(tmp_path) -> None:
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
    assert profile["preferences"].get("response_style") in (None, "")
    assert profile["preferences"].get("query_scope") in (None, "")
    assert profile["skillspec"].get("confirm_preference") in (None, "")
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


@pytest.mark.asyncio
async def test_connect_command_reports_oauth_not_enabled_when_service_missing(tmp_path) -> None:
    loop, provider = _build_loop(tmp_path)
    store = UserMemoryStore(tmp_path)
    store.write(
        "feishu",
        "ou_connect",
        {
            "identity": {"name": "张三", "role": "lawyer"},
            "preferences": {},
            "dynamic": {},
            "skillspec": {},
            "onboarding": {"status": "completed", "step": "completed"},
        },
    )

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_connect",
            chat_id="oc_group",
            content="/connect",
            metadata={"chat_type": "group", "message_id": "m-connect"},
        )
    )

    assert response is not None
    assert "未启用飞书 OAuth 回调服务" in response.content
    assert provider.calls == 0
