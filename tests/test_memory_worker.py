import pytest

from nanobot.agent.memory_worker import MemoryTurnTask, MemoryWriteWorker


@pytest.mark.asyncio
async def test_memory_worker_writes_scoped_memory_files(tmp_path) -> None:
    worker = MemoryWriteWorker(tmp_path)
    await worker.start()

    await worker.enqueue(
        MemoryTurnTask(
            channel="feishu",
            user_id="ou_user_1",
            chat_id="oc_chat_1",
            thread_id="omt_thread_1",
            user_text="请跟进这个事项",
            assistant_text="好的，我会跟进。",
            message_id="om_1",
            scopes=("chat", "thread"),
        )
    )
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

    chat_memory = (tmp_path / "memory" / "feishu" / "chats" / "oc_chat_1" / "MEMORY.md").read_text(
        encoding="utf-8"
    )
    thread_memory = (
        tmp_path / "memory" / "feishu" / "threads" / "oc_chat_1__omt_thread_1" / "MEMORY.md"
    ).read_text(encoding="utf-8")
    user_memory = (tmp_path / "memory" / "feishu" / "users" / "ou_user_2" / "MEMORY.md").read_text(
        encoding="utf-8"
    )

    assert "请跟进这个事项" in chat_memory
    assert "请跟进这个事项" in thread_memory
    assert "我的偏好是简洁回答" in user_memory


@pytest.mark.asyncio
async def test_memory_worker_deduplicates_same_turn(tmp_path) -> None:
    worker = MemoryWriteWorker(tmp_path)
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
