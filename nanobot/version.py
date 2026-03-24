"""Version helpers for nanobot."""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

__version__ = "0.3.5"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _normalize_commit(value: str | None) -> str | None:
    commit = (value or "").strip()
    if not commit:
        return None
    return commit[:7]


def _read_git_revision_from_env() -> str | None:
    for name in ("NANOBOT_GIT_COMMIT", "GIT_COMMIT", "SOURCE_COMMIT"):
        if commit := _normalize_commit(os.environ.get(name)):
            return commit
    return None


def _read_git_revision_from_repo() -> str | None:
    repo_root = _repo_root()
    if not (repo_root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=1.5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return _normalize_commit(result.stdout)


@lru_cache(maxsize=1)
def get_git_revision() -> str | None:
    return _read_git_revision_from_env() or _read_git_revision_from_repo()


def get_version_info(app_name: str = "nanobot") -> dict[str, str | None]:
    commit = get_git_revision()
    display = f"{app_name} v{__version__}"
    if commit:
        display = f"{display} ({commit})"
    return {
        "name": app_name,
        "version": __version__,
        "git_revision": commit,
        "display": display,
    }


def format_version(app_name: str = "nanobot") -> str:
    return str(get_version_info(app_name)["display"])
