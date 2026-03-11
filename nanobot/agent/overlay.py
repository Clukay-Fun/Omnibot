"""Typed overlay context passed across message, session, and heartbeat paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(slots=True)
class OverlayContext:
    """Serializable overlay information for a user-scoped workspace."""

    METADATA_KEY = "_overlay_context"

    system_overlay_root: str | None = None
    system_overlay_bootstrap: bool | None = None

    @property
    def root_path(self) -> Path | None:
        """Return the overlay root as a Path if configured."""
        if not self.system_overlay_root:
            return None
        return Path(self.system_overlay_root).expanduser()

    def is_empty(self) -> bool:
        """Whether this overlay carries any routing information."""
        return self.system_overlay_root is None and self.system_overlay_bootstrap is None

    def to_metadata(self, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Merge this overlay context into a metadata dict."""
        merged = dict(metadata or {})
        if self.is_empty():
            merged.pop(self.METADATA_KEY, None)
            merged.pop("system_overlay_root", None)
            merged.pop("system_overlay_bootstrap", None)
            return merged

        payload = {
            "system_overlay_root": self.system_overlay_root,
            "system_overlay_bootstrap": self.system_overlay_bootstrap,
        }
        merged[self.METADATA_KEY] = payload
        # Keep the flat keys for backward compatibility with older sessions/tests.
        if self.system_overlay_root is not None:
            merged["system_overlay_root"] = self.system_overlay_root
        else:
            merged.pop("system_overlay_root", None)
        if self.system_overlay_bootstrap is not None:
            merged["system_overlay_bootstrap"] = self.system_overlay_bootstrap
        else:
            merged.pop("system_overlay_bootstrap", None)
        return merged

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any] | None) -> "OverlayContext":
        """Parse overlay context from message/session metadata."""
        if not metadata:
            return cls()

        raw = metadata.get(cls.METADATA_KEY)
        if isinstance(raw, Mapping):
            return cls(
                system_overlay_root=_as_optional_str(raw.get("system_overlay_root")),
                system_overlay_bootstrap=_as_optional_bool(raw.get("system_overlay_bootstrap")),
            )

        return cls(
            system_overlay_root=_as_optional_str(metadata.get("system_overlay_root")),
            system_overlay_bootstrap=_as_optional_bool(metadata.get("system_overlay_bootstrap")),
        )


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _as_optional_bool(value: Any) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return bool(value)
