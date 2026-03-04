from nanobot.agent.skill_runtime.user_memory import UserMemoryStore


def test_user_memory_path_and_rw(tmp_path) -> None:
    store = UserMemoryStore(tmp_path)

    path = store.path_for("slack", "U123")
    assert path == tmp_path / "memory" / "users" / "slack__U123.json"

    store.write("slack", "U123", {"name": "Ada"})
    loaded = store.read("slack", "U123")

    assert loaded == {"name": "Ada"}


def test_user_memory_update_merges_payload(tmp_path) -> None:
    store = UserMemoryStore(tmp_path)

    store.write("telegram", "42", {"city": "Shenzhen", "lang": "en"})
    updated = store.update("telegram", "42", {"lang": "zh", "team": "ops"})

    assert updated == {"city": "Shenzhen", "lang": "zh", "team": "ops"}
