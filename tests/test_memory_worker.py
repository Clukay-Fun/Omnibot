import asyncio

import pytest

from nanobot.agent.memory_worker import MemoryTurnTask, MemoryWriteWorker


def _chat_task(*, message_id: str, force_flush: bool = False) -> MemoryTurnTask:
    return MemoryTurnTask(
        channel="feishu",
        user_id="ou_user_1",
        chat_id="oc_chat_1",
        thread_id="omt_thread_1",
        user_text=f"用户输入-{message_id}",
        assistant_text=f"助手回复-{message_id}",
        message_id=message_id,
        scopes=("chat", "thread"),
        force_flush=force_flush,
    )


@pytest.mark.asyncio
async def test_memory_worker_does_not_persist_before_threshold(tmp_path) -> None:
    worker = MemoryWriteWorker(tmp_path, flush_threshold=2)
    await worker.start()

    await worker.enqueue(_chat_task(message_id="om_1"))
    await asyncio.sleep(0.05)

    path = tmp_path / "memory" / "feishu" / "chats" / "oc_chat_1" / "MEMORY.md"
    assert not path.exists()

    await worker.stop()


@pytest.mark.asyncio
async def test_memory_worker_flushes_when_threshold_reached(tmp_path) -> None:
    worker = MemoryWriteWorker(tmp_path, flush_threshold=2)
    await worker.start()

    await worker.enqueue(_chat_task(message_id="om_1"))
    await worker.enqueue(_chat_task(message_id="om_2"))
    await asyncio.sleep(0.05)

    chat_path = tmp_path / "memory" / "feishu" / "chats" / "oc_chat_1" / "MEMORY.md"
    thread_path = tmp_path / "memory" / "feishu" / "threads" / "oc_chat_1__omt_thread_1" / "MEMORY.md"
    assert chat_path.exists()
    assert thread_path.exists()

    chat_memory = chat_path.read_text(encoding="utf-8")
    thread_memory = thread_path.read_text(encoding="utf-8")
    assert "用户输入-om_1" in chat_memory
    assert "用户输入-om_2" in chat_memory
    assert "用户输入-om_1" in thread_memory
    assert "用户输入-om_2" in thread_memory

    await worker.stop()


@pytest.mark.asyncio
async def test_memory_worker_force_flush_writes_immediately(tmp_path) -> None:
    worker = MemoryWriteWorker(tmp_path, flush_threshold=10)
    await worker.start()

    await worker.enqueue(_chat_task(message_id="om_force", force_flush=True))
    await asyncio.sleep(0.05)

    path = tmp_path / "memory" / "feishu" / "chats" / "oc_chat_1" / "MEMORY.md"
    assert path.exists()
    assert "用户输入-om_force" in path.read_text(encoding="utf-8")

    await worker.stop()


@pytest.mark.asyncio
async def test_memory_worker_flushes_buffer_on_stop(tmp_path) -> None:
    worker = MemoryWriteWorker(tmp_path, flush_threshold=10)
    await worker.start()

    await worker.enqueue(_chat_task(message_id="om_stop"))
    await asyncio.sleep(0.05)

    path = tmp_path / "memory" / "feishu" / "chats" / "oc_chat_1" / "MEMORY.md"
    assert not path.exists()

    await worker.stop()

    assert path.exists()
    assert "用户输入-om_stop" in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_memory_worker_writes_user_scope_memory(tmp_path) -> None:
    worker = MemoryWriteWorker(tmp_path, flush_threshold=1)
    await worker.start()

    await worker.enqueue(
        MemoryTurnTask(
            channel="feishu",
            user_id="ou_user_2",
            chat_id="ou_user_2",
            thread_id=None,
            user_text="我的偏好是简洁回答",
            assistant_text="收到，后续我会保持简洁。",
            message_id="om_2",
            scopes=("user",),
        )
    )
    await worker.stop()

    user_memory = (tmp_path / "memory" / "feishu" / "users" / "ou_user_2" / "MEMORY.md").read_text(
        encoding="utf-8"
    )
    assert "我的偏好是简洁回答" in user_memory


@pytest.mark.asyncio
async def test_memory_worker_deduplicates_same_turn(tmp_path) -> None:
    worker = MemoryWriteWorker(tmp_path, flush_threshold=1)
    await worker.start()

    task = MemoryTurnTask(
        channel="feishu",
        user_id="ou_user_1",
        chat_id="oc_chat_1",
        thread_id=None,
        user_text="记录一下今天进展",
        assistant_text="已记录。",
        message_id="om_dup",
        scopes=("chat",),
    )

    await worker.enqueue(task)
    await worker.enqueue(task)
    await worker.stop()

    path = tmp_path / "memory" / "feishu" / "chats" / "oc_chat_1" / "MEMORY.md"
    content = path.read_text(encoding="utf-8")

    assert content.count("<!-- turn:") == 1
    assert content.count("记录一下今天进展") == 1


@pytest.mark.asyncio
async def test_memory_worker_respects_task_level_flush_threshold(tmp_path) -> None:
    worker = MemoryWriteWorker(tmp_path, flush_threshold=10)
    await worker.start()

    await worker.enqueue(
        MemoryTurnTask(
            channel="feishu",
            user_id="ou_user_1",
            chat_id="oc_chat_1",
            thread_id=None,
            user_text="第一条",
            assistant_text="收到",
            message_id="om_t1",
            scopes=("chat",),
            flush_threshold=2,
        )
    )
    await worker.enqueue(
        MemoryTurnTask(
            channel="feishu",
            user_id="ou_user_1",
            chat_id="oc_chat_1",
            thread_id=None,
            user_text="第二条",
            assistant_text="继续",
            message_id="om_t2",
            scopes=("chat",),
            flush_threshold=2,
        )
    )
    await asyncio.sleep(0.05)

    path = tmp_path / "memory" / "feishu" / "chats" / "oc_chat_1" / "MEMORY.md"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "第一条" in content
    assert "第二条" in content

    await worker.stop()
