import sqlite3

import pytest

from nanobot.storage.audit import AuditSink
from nanobot.storage.sqlite_store import SQLiteStore


@pytest.mark.asyncio
async def test_audit_sink_flushes_queue_to_sqlite(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "memory" / "feishu" / "state.sqlite3")
    sink = AuditSink(store, batch_size=2, flush_interval_seconds=0.05)
    await sink.start()

    await sink.log_event("agent_request_started", chat_id="oc_1", message_id="om_1", payload={"step": 1})
    await sink.log_event("agent_request_finished", chat_id="oc_1", message_id="om_1", payload={"step": 2})
    await sink.log_event("agent_request_started", chat_id="oc_2", message_id="om_2", payload={"step": 3})

    await sink.stop()

    conn = sqlite3.connect(str(store.db_path))
    rows = conn.execute(
        "SELECT event_type, chat_id, message_id FROM event_audit ORDER BY id ASC"
    ).fetchall()
    conn.close()

    assert rows == [
        ("agent_request_started", "oc_1", "om_1"),
        ("agent_request_finished", "oc_1", "om_1"),
        ("agent_request_started", "oc_2", "om_2"),
    ]


@pytest.mark.asyncio
async def test_audit_sink_stop_flushes_remaining_events(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "memory" / "feishu" / "state.sqlite3")
    sink = AuditSink(store, batch_size=50, flush_interval_seconds=10.0)
    await sink.start()

    await sink.log_event("agent_request_error", chat_id="oc_3", message_id="om_3", payload={"error": "boom"})
    await sink.stop()

    conn = sqlite3.connect(str(store.db_path))
    count = conn.execute("SELECT COUNT(*) FROM event_audit WHERE event_type='agent_request_error'").fetchone()[0]
    conn.close()

    assert count == 1
