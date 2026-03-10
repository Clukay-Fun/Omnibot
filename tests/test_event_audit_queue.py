import asyncio
import sqlite3
from datetime import datetime, timedelta

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


def test_sqlite_event_audit_query_filters(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "memory" / "feishu" / "state.sqlite3")
    base = datetime(2025, 1, 1, 0, 0, 0)
    store.record_event_audit_batch(
        [
            {
                "event_type": "agent_request_started",
                "event_id": "evt_1",
                "chat_id": "oc_1",
                "message_id": "om_1",
                "payload": {"step": 1},
                "created_at": base.isoformat(),
            },
            {
                "event_type": "agent_request_finished",
                "event_id": "evt_2",
                "chat_id": "oc_1",
                "message_id": "om_1",
                "payload": {"step": 2},
                "created_at": (base + timedelta(minutes=1)).isoformat(),
            },
            {
                "event_type": "agent_request_started",
                "event_id": "evt_3",
                "chat_id": "oc_2",
                "message_id": "om_2",
                "payload": {"step": 3},
                "created_at": (base + timedelta(minutes=2)).isoformat(),
            },
        ]
    )

    by_type = store.query_event_audit(event_type="agent_request_started")
    assert [item["event_id"] for item in by_type] == ["evt_3", "evt_1"]

    by_chat_and_message = store.query_event_audit(chat_id="oc_1", message_id="om_1")
    assert [item["event_id"] for item in by_chat_and_message] == ["evt_2", "evt_1"]

    in_time_range = store.query_event_audit(
        start_at=(base + timedelta(seconds=30)).isoformat(),
        end_at=(base + timedelta(minutes=1, seconds=30)).isoformat(),
    )
    assert [item["event_id"] for item in in_time_range] == ["evt_2"]

    paged = store.query_event_audit(limit=1, offset=1)
    assert [item["event_id"] for item in paged] == ["evt_2"]


def test_sqlite_cleanup_returns_deleted_counts(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "memory" / "feishu" / "state.sqlite3")
    base = datetime(2025, 1, 1, 0, 0, 0)
    store.record_event_audit_batch(
        [
            {
                "event_type": "event_old",
                "event_id": "evt_old",
                "chat_id": "oc_1",
                "message_id": "om_1",
                "payload": {},
                "created_at": base.isoformat(),
            },
            {
                "event_type": "event_new",
                "event_id": "evt_new",
                "chat_id": "oc_1",
                "message_id": "om_2",
                "payload": {},
                "created_at": (base + timedelta(days=10)).isoformat(),
            },
        ]
    )
    store.upsert_feishu_message_index(
        "m_old",
        chat_id="oc_1",
        content="old",
        source_message_id=None,
        created_at=base.isoformat(),
    )
    store.upsert_feishu_message_index(
        "m_new",
        chat_id="oc_1",
        content="new",
        source_message_id=None,
        created_at=(base + timedelta(days=10)).isoformat(),
    )

    cutoff = (base + timedelta(days=1)).isoformat()
    deleted_audit = store.cleanup_event_audit_before(cutoff)
    deleted_index = store.cleanup_feishu_message_index_before(cutoff)

    assert deleted_audit == 1
    assert deleted_index == 1
    assert [item["event_id"] for item in store.query_event_audit()] == ["evt_new"]
    assert store.get_feishu_message_index("m_old") is None
    assert store.get_feishu_message_index("m_new") is not None


def test_audit_sink_exposes_query_and_cleanup_wrappers(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "memory" / "feishu" / "state.sqlite3")
    sink = AuditSink(store, enable_cleanup_task=False)
    created_at = datetime(2025, 1, 1, 0, 0, 0).isoformat()

    store.record_event_audit_batch(
        [
            {
                "event_type": "agent_request_started",
                "event_id": "evt_1",
                "chat_id": "oc_1",
                "message_id": "om_1",
                "payload": {"step": 1},
                "created_at": created_at,
            }
        ]
    )

    rows = sink.query_event_audit(event_type="agent_request_started")
    assert [item["event_id"] for item in rows] == ["evt_1"]

    deleted = sink.cleanup_event_audit_before((datetime(2025, 1, 2, 0, 0, 0)).isoformat())
    assert deleted == 1


@pytest.mark.asyncio
async def test_audit_sink_cleanup_worker_runs_periodically(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path / "memory" / "feishu" / "state.sqlite3")
    sink = AuditSink(
        store,
        cleanup_interval_seconds=0.05,
        event_audit_retention_days=30,
        feishu_message_index_retention_days=30,
    )

    calls = {"oauth": 0, "audit": 0, "index": 0}

    def fake_cleanup_expired_oauth_states(*, now_iso: str) -> int:
        calls["oauth"] += 1
        return 1

    def fake_cleanup_event_audit_before(before_at: str) -> int:
        calls["audit"] += 1
        return 2

    def fake_cleanup_feishu_message_index_before(before_at: str) -> int:
        calls["index"] += 1
        return 3

    monkeypatch.setattr(store, "cleanup_expired_oauth_states", fake_cleanup_expired_oauth_states)
    monkeypatch.setattr(store, "cleanup_event_audit_before", fake_cleanup_event_audit_before)
    monkeypatch.setattr(store, "cleanup_feishu_message_index_before", fake_cleanup_feishu_message_index_before)

    await sink.start()
    await asyncio.sleep(0.16)
    await sink.stop()

    assert calls["oauth"] >= 2
    assert calls["audit"] >= 2
    assert calls["index"] >= 2
