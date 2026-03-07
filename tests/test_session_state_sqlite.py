import json
from datetime import datetime, timedelta

from nanobot.session.manager import SessionManager


def test_session_state_saved_to_sqlite_and_loaded_preferentially(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("feishu:oc_chat_1")
    session.created_at = datetime.now() - timedelta(days=1)
    session.updated_at = datetime.now()
    session.metadata = {"topic": "from_sqlite"}
    session.last_consolidated = 7
    manager.save(session)

    path = manager._get_session_path(session.key)
    lines = path.read_text(encoding="utf-8").splitlines()
    metadata_line = json.loads(lines[0])
    metadata_line["metadata"] = {"topic": "from_jsonl"}
    metadata_line["last_consolidated"] = 1
    lines[0] = json.dumps(metadata_line, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    manager.invalidate(session.key)
    loaded = manager.get_or_create("feishu:oc_chat_1")

    assert loaded.metadata == {"topic": "from_sqlite"}
    assert loaded.last_consolidated == 7


def test_list_sessions_uses_sqlite_updated_at(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("feishu:oc_chat_2")
    session.metadata = {"name": "demo"}
    session.updated_at = datetime(2026, 1, 2, 3, 4, 5)
    manager.save(session)

    path = manager._get_session_path(session.key)
    lines = path.read_text(encoding="utf-8").splitlines()
    metadata_line = json.loads(lines[0])
    metadata_line["updated_at"] = "2020-01-01T00:00:00"
    lines[0] = json.dumps(metadata_line, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    sessions = manager.list_sessions()
    row = next(item for item in sessions if item["key"] == "feishu:oc_chat_2")

    assert row["updated_at"] == "2026-01-02T03:04:05"
