from nanobot.utils.helpers import migrate_legacy_path


def test_migrate_legacy_path_moves_sidecars_from_stem_paths(tmp_path):
    source = tmp_path / "workspace" / "reminders.json"
    target = tmp_path / "state" / "reminders.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("[]", encoding="utf-8")
    source.with_suffix(".sqlite3").write_text("db", encoding="utf-8")
    source.with_suffix(".sqlite3-wal").write_text("wal", encoding="utf-8")

    moved = migrate_legacy_path(
        source,
        target,
        related_suffixes=(".sqlite3", ".sqlite3-wal", ".sqlite3-shm"),
    )

    assert moved is True
    assert target.exists()
    assert target.with_suffix(".sqlite3").read_text(encoding="utf-8") == "db"
    assert target.with_suffix(".sqlite3-wal").read_text(encoding="utf-8") == "wal"
    assert not source.with_suffix(".sqlite3").exists()
