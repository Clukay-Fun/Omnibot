import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config
from nanobot.providers.base import LLMResponse


class _DummyProvider:
    async def chat(self, **kwargs):
        return LLMResponse(content="ok", tool_calls=[])

    def get_default_model(self) -> str:
        return "dummy"


class _CaptureMemoryWorker:
    def __init__(self) -> None:
        self.tasks = []

    async def enqueue(self, task) -> None:
        self.tasks.append(task)


@pytest.mark.asyncio
async def test_memory_trigger_force_flush_on_topic_end_keyword(tmp_path) -> None:
    loop = AgentLoop(bus=MessageBus(), provider=_DummyProvider(), workspace=tmp_path)
    capture = _CaptureMemoryWorker()
    loop._memory_worker = capture

    msg = InboundMessage(
        channel="feishu",
        sender_id="ou_user_1",
        chat_id="oc_group_1",
        content="这次先这样",
        metadata={"chat_type": "group", "thread_id": "omt_topic_1", "message_id": "om_1"},
    )

    await loop._enqueue_memory_write(msg, "收到")

    assert len(capture.tasks) == 1
    assert capture.tasks[0].force_flush is True
    assert capture.tasks[0].scopes == ("chat", "thread")


@pytest.mark.asyncio
async def test_memory_trigger_keeps_threshold_mode_for_regular_thread_messages(tmp_path) -> None:
    loop = AgentLoop(bus=MessageBus(), provider=_DummyProvider(), workspace=tmp_path)
    capture = _CaptureMemoryWorker()
    loop._memory_worker = capture

    msg = InboundMessage(
        channel="feishu",
        sender_id="ou_user_1",
        chat_id="oc_group_1",
        content="继续推进这个方案",
        metadata={"chat_type": "group", "thread_id": "omt_topic_1", "message_id": "om_2"},
    )

    await loop._enqueue_memory_write(msg, "继续中")

    assert len(capture.tasks) == 1
    assert capture.tasks[0].force_flush is False
    assert capture.tasks[0].scopes == ("chat", "thread")


@pytest.mark.asyncio
async def test_memory_trigger_does_not_force_flush_without_thread_context(tmp_path) -> None:
    loop = AgentLoop(bus=MessageBus(), provider=_DummyProvider(), workspace=tmp_path)
    capture = _CaptureMemoryWorker()
    loop._memory_worker = capture

    msg = InboundMessage(
        channel="feishu",
        sender_id="ou_user_1",
        chat_id="oc_group_1",
        content="done",
        metadata={"chat_type": "group", "message_id": "om_3"},
    )

    await loop._enqueue_memory_write(msg, "收到")

    assert len(capture.tasks) == 1
    assert capture.tasks[0].force_flush is False
    assert capture.tasks[0].scopes == ("chat",)


@pytest.mark.asyncio
async def test_memory_trigger_uses_group_flush_threshold_from_config(tmp_path) -> None:
    config = Config.model_validate(
        {
            "channels": {
                "feishu": {
                    "memoryFlushThresholdPrivate": 2,
                    "memoryFlushThresholdGroup": 7,
                    "memoryForceFlushOnTopicEnd": False,
                }
            }
        }
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_DummyProvider(),
        workspace=tmp_path,
        channels_config=config.channels,
    )
    capture = _CaptureMemoryWorker()
    loop._memory_worker = capture

    msg = InboundMessage(
        channel="feishu",
        sender_id="ou_user_1",
        chat_id="oc_group_1",
        content="done",
        metadata={"chat_type": "group", "thread_id": "omt_topic_1", "message_id": "om_4"},
    )

    await loop._enqueue_memory_write(msg, "收到")

    assert len(capture.tasks) == 1
    assert capture.tasks[0].flush_threshold == 7
    assert capture.tasks[0].force_flush is False


@pytest.mark.asyncio
async def test_memory_trigger_uses_private_flush_threshold_from_config(tmp_path) -> None:
    config = Config.model_validate(
        {
            "channels": {
                "feishu": {
                    "memoryFlushThresholdPrivate": 2,
                    "memoryFlushThresholdGroup": 7,
                }
            }
        }
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_DummyProvider(),
        workspace=tmp_path,
        channels_config=config.channels,
    )
    capture = _CaptureMemoryWorker()
    loop._memory_worker = capture

    msg = InboundMessage(
        channel="feishu",
        sender_id="ou_user_1",
        chat_id="ou_user_1",
        content="继续",
        metadata={"chat_type": "p2p", "message_id": "om_5"},
    )

    await loop._enqueue_memory_write(msg, "收到")

    assert len(capture.tasks) == 1
    assert capture.tasks[0].scopes == ("user",)
    assert capture.tasks[0].flush_threshold == 2
