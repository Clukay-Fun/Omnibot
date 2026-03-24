from __future__ import annotations

import tomllib
from pathlib import Path

from nanobot import __version__
from nanobot.version import format_version, get_git_revision


def test_pyproject_uses_dynamic_version_from_single_source() -> None:
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert data["project"]["dynamic"] == ["version"]
    assert "version" not in data["project"]
    assert data["tool"]["hatch"]["version"]["path"] == "nanobot/version.py"
    assert __version__ == "0.3.5"


def test_format_version_prefers_env_commit(monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_GIT_COMMIT", "abcdef123456")
    get_git_revision.cache_clear()

    assert format_version() == "nanobot v0.3.5 (abcdef1)"

    get_git_revision.cache_clear()


def test_format_version_omits_commit_when_unavailable(monkeypatch) -> None:
    monkeypatch.delenv("NANOBOT_GIT_COMMIT", raising=False)
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("SOURCE_COMMIT", raising=False)
    monkeypatch.setattr("nanobot.version._read_git_revision_from_repo", lambda: None)
    get_git_revision.cache_clear()

    assert format_version() == "nanobot v0.3.5"

    get_git_revision.cache_clear()
