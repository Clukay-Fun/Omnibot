"""Helpers for loading the shared workspace directory mapping config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_directory_config(workspace: Path | None) -> dict[str, Any]:
    if workspace is None:
        return {}
    path = workspace / "feishu" / "bitable_rules.yaml"
    if not path.exists():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    directory = payload.get("directory") if isinstance(payload, dict) else {}
    return dict(directory) if isinstance(directory, dict) else {}
