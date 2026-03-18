"""Tests for /stop task cancellation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_loop():
    """Create a minimal AgentLoop with mocked dependencies."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())

    with patch("nanobot.agent.loop.ContextBuilder"), \
         patch("nanobot.agent.loop.SessionManager"), \
         patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace)
    return loop, bus


class TestHandleStop:
    @pytest.mark.asyncio
    async def test_stop_no_active_task(self):
        from nanobot.bus.events import InboundMessage

        loop, bus = _make_loop()
        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="/stop")
        await loop._handle_stop(msg)
        out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert "No active task" in out.content

    @pytest.mark.asyncio
    async def test_stop_cancels_active_task(self):
        from nanobot.bus.events import InboundMessage

        loop, bus = _make_loop()
        cancelled = asyncio.Event()

        async def slow_task():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(slow_task())
        await asyncio.sleep(0)
        loop._active_tasks["test:c1"] = [task]

        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="/stop")
        await loop._handle_stop(msg)

        assert cancelled.is_set()
        out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert "stopped" in out.content.lower()

    @pytest.mark.asyncio
    async def test_stop_cancels_multiple_tasks(self):
        from nanobot.bus.events import InboundMessage

        loop, bus = _make_loop()
        events = [asyncio.Event(), asyncio.Event()]

        async def slow(idx):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                events[idx].set()
                raise

        tasks = [asyncio.create_task(slow(i)) for i in range(2)]
        await asyncio.sleep(0)
        loop._active_tasks["test:c1"] = tasks

        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="/stop")
        await loop._handle_stop(msg)

        assert all(e.is_set() for e in events)
        out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert "2 task" in out.content


class TestDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_processes_and_publishes(self):
        from nanobot.bus.events import InboundMessage, OutboundMessage

        loop, bus = _make_loop()
        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="hello")
        loop._process_message = AsyncMock(
            return_value=OutboundMessage(channel="test", chat_id="c1", content="hi")
        )
        await loop._dispatch(msg)
        out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert out.content == "hi"

    @pytest.mark.asyncio
    async def test_same_session_serializes(self):
        from nanobot.bus.events import InboundMessage, OutboundMessage

        loop, bus = _make_loop()
        order: list[str] = []
        release = asyncio.Event()
        first_started = asyncio.Event()
        second_started = asyncio.Event()

        async def mock_process(m, **kwargs):
            order.append(f"start-{m.content}")
            if m.content == "a":
                first_started.set()
                await release.wait()
            else:
                second_started.set()
            order.append(f"end-{m.content}")
            return OutboundMessage(channel="test", chat_id="c1", content=m.content)

        loop._process_message = mock_process
        msg1 = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="a")
        msg2 = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="b")

        t1 = asyncio.create_task(loop._dispatch(msg1))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        t2 = asyncio.create_task(loop._dispatch(msg2))

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(second_started.wait(), timeout=0.05)

        release.set()
        await asyncio.gather(t1, t2)
        assert order == ["start-a", "end-a", "start-b", "end-b"]

        await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_different_sessions_can_run_concurrently(self):
        from nanobot.bus.events import InboundMessage, OutboundMessage

        loop, bus = _make_loop()
        order: list[str] = []
        release = asyncio.Event()
        first_started = asyncio.Event()
        second_started = asyncio.Event()

        async def mock_process(m, **kwargs):
            order.append(f"start-{m.chat_id}")
            if m.chat_id == "c1":
                first_started.set()
                await release.wait()
            else:
                second_started.set()
                await release.wait()
            order.append(f"end-{m.chat_id}")
            return OutboundMessage(channel="test", chat_id=m.chat_id, content=m.content)

        loop._process_message = mock_process
        msg1 = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="a")
        msg2 = InboundMessage(channel="test", sender_id="u2", chat_id="c2", content="b")

        t1 = asyncio.create_task(loop._dispatch(msg1))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        t2 = asyncio.create_task(loop._dispatch(msg2))

        await asyncio.wait_for(second_started.wait(), timeout=1.0)
        assert order[:2] == ["start-c1", "start-c2"]

        release.set()
        await asyncio.gather(t1, t2)

        await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_process_direct_serializes_by_session_key(self):
        loop, _ = _make_loop()
        order: list[str] = []
        release = asyncio.Event()
        first_started = asyncio.Event()
        second_started = asyncio.Event()

        async def mock_process(m, session_key=None, on_progress=None):
            order.append(f"start-{m.content}-{session_key}")
            if m.content == "first":
                first_started.set()
                await release.wait()
            else:
                second_started.set()
            order.append(f"end-{m.content}-{session_key}")
            return type("Resp", (), {"content": m.content})()

        loop._process_message = mock_process

        t1 = asyncio.create_task(loop.process_direct("first", session_key="cli:shared"))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        t2 = asyncio.create_task(loop.process_direct("second", session_key="cli:shared"))

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(second_started.wait(), timeout=0.05)

        release.set()
        results = await asyncio.gather(t1, t2)

        assert results == ["first", "second"]
        assert order == [
            "start-first-cli:shared",
            "end-first-cli:shared",
            "start-second-cli:shared",
            "end-second-cli:shared",
        ]


class TestSubagentCancellation:
    @pytest.mark.asyncio
    async def test_cancel_by_session(self):
        from nanobot.agent.subagent import SubagentManager
        from nanobot.bus.queue import MessageBus

        bus = MessageBus()
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"
        mgr = SubagentManager(provider=provider, workspace=MagicMock(), bus=bus)

        cancelled = asyncio.Event()

        async def slow():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(slow())
        await asyncio.sleep(0)
        mgr._running_tasks["sub-1"] = task
        mgr._session_tasks["test:c1"] = {"sub-1"}

        count = await mgr.cancel_by_session("test:c1")
        assert count == 1
        assert cancelled.is_set()

    @pytest.mark.asyncio
    async def test_cancel_by_session_no_tasks(self):
        from nanobot.agent.subagent import SubagentManager
        from nanobot.bus.queue import MessageBus

        bus = MessageBus()
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"
        mgr = SubagentManager(provider=provider, workspace=MagicMock(), bus=bus)
        assert await mgr.cancel_by_session("nonexistent") == 0
