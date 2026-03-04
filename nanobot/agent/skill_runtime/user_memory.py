"""Per-user memory profile store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobot.utils.helpers import ensure_dir, safe_filename


class UserMemoryStore:
    """File-based user memory store keyed by channel+sender id."""

    def __init__(self, workspace: Path):
        self.root = ensure_dir(workspace / "memory" / "users")

    def path_for(self, channel: str, sender_id: str) -> Path:
        channel_key = safe_filename(channel)
        sender_key = safe_filename(sender_id)
        return self.root / f"{channel_key}__{sender_key}.json"

    def read(self, channel: str, sender_id: str) -> dict[str, Any]:
        path = self.path_for(channel, sender_id)
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, channel: str, sender_id: str, profile: dict[str, Any]) -> Path:
        path = self.path_for(channel, sender_id)
        path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def update(self, channel: str, sender_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.read(channel, sender_id)
        current.update(patch)
        self.write(channel, sender_id, current)
        return current
