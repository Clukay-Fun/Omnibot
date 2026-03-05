"""Skillspec registry with workspace override support."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from yaml import YAMLError

from nanobot.agent.skill_runtime.spec_schema import SkillSpec


@dataclass(slots=True)
class SkillSpecRegistryReport:
    loaded: list[str] = field(default_factory=list)
    overridden: list[str] = field(default_factory=list)
    source_collisions: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _SkillSpecRecord:
    name: str
    spec: SkillSpec
    path: Path
    source: str


class SkillSpecRegistry:
    """Load and validate skillspec files from bundled + managed + workspace roots."""

    def __init__(self, workspace_root: Path, builtin_root: Path | None = None):
        self.workspace_root = workspace_root
        self.managed_root = workspace_root / "managed"
        self.builtin_root = builtin_root or Path(__file__).resolve().parents[2] / "skills" / "skillspec"
        self.report = SkillSpecRegistryReport()
        self._specs: dict[str, _SkillSpecRecord] = {}

    @property
    def specs(self) -> dict[str, SkillSpec]:
        return {name: record.spec for name, record in self._specs.items()}

    def load(self) -> dict[str, SkillSpec]:
        self.report = SkillSpecRegistryReport()
        layers = [
            ("builtin", self._load_root(self.builtin_root, source="builtin")),
            ("managed", self._load_root(self.managed_root, source="managed")),
            ("workspace", self._load_root(self.workspace_root, source="workspace")),
        ]

        merged: dict[str, _SkillSpecRecord] = {}
        for source, records in layers:
            for name, record in records.items():
                previous = merged.get(name)
                if previous is not None:
                    self.report.overridden.append(name)
                    self.report.source_collisions.append(
                        f"{name}: {previous.source}:{previous.path.name} -> {source}:{record.path.name}"
                    )
                merged[name] = record

        active: dict[str, _SkillSpecRecord] = {}
        for name, record in merged.items():
            if not record.spec.meta.enabled:
                self.report.disabled.append(name)
                continue
            active[name] = record

        self._specs = active
        self.report.loaded = sorted(active)
        self.report.overridden = sorted(set(self.report.overridden))
        self.report.source_collisions.sort()
        self.report.disabled.sort()
        self.report.invalid.sort()
        return self.specs

    def _load_root(self, root: Path, source: str) -> dict[str, _SkillSpecRecord]:
        records: dict[str, _SkillSpecRecord] = {}
        if not root.exists():
            return records

        candidates = sorted(list(root.glob("*.yaml")) + list(root.glob("*.yml")))
        for path in candidates:
            name = path.stem
            try:
                raw = self._safe_load_yaml(path)
                if not isinstance(raw, dict):
                    raise ValueError("skillspec must be a YAML object")
                spec = SkillSpec.model_validate(raw)
                records[name] = _SkillSpecRecord(name=name, spec=spec, path=path, source=source)
            except Exception as exc:
                self.report.invalid.append(f"{source}:{name} ({exc})")
        return records

    @staticmethod
    def _safe_load_yaml(path: Path) -> object:
        """Load YAML with a regex-friendly fallback for backslash escapes.

        Many skill authors write regex in double quotes (e.g. "\\s+"), which can
        trigger YAML escape parsing errors. If that happens, retry by escaping raw
        backslashes so regex literals remain intact.
        """
        text = path.read_text(encoding="utf-8")
        try:
            return yaml.safe_load(text)
        except YAMLError as exc:
            if "unknown escape character" not in str(exc):
                raise
            escaped_text = text.replace("\\", "\\\\")
            return yaml.safe_load(escaped_text)
