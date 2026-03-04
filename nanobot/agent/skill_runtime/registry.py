"""Skillspec registry with workspace override support."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from nanobot.agent.skill_runtime.spec_schema import SkillSpec


@dataclass(slots=True)
class SkillSpecRegistryReport:
    loaded: list[str] = field(default_factory=list)
    overridden: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _SkillSpecRecord:
    name: str
    spec: SkillSpec
    path: Path
    source: str


class SkillSpecRegistry:
    """Load and validate skillspec files from builtin + workspace roots."""

    def __init__(self, workspace_root: Path, builtin_root: Path | None = None):
        self.workspace_root = workspace_root
        self.builtin_root = builtin_root or Path(__file__).resolve().parents[2] / "skills" / "skillspec"
        self.report = SkillSpecRegistryReport()
        self._specs: dict[str, _SkillSpecRecord] = {}

    @property
    def specs(self) -> dict[str, SkillSpec]:
        return {name: record.spec for name, record in self._specs.items()}

    def load(self) -> dict[str, SkillSpec]:
        self.report = SkillSpecRegistryReport()
        builtin = self._load_root(self.builtin_root, source="builtin")
        workspace = self._load_root(self.workspace_root, source="workspace")

        merged: dict[str, _SkillSpecRecord] = dict(builtin)
        for name, record in workspace.items():
            if name in merged:
                self.report.overridden.append(name)
            merged[name] = record

        active: dict[str, _SkillSpecRecord] = {}
        for name, record in merged.items():
            if not record.spec.meta.enabled:
                self.report.disabled.append(name)
                continue
            active[name] = record

        self._specs = active
        self.report.loaded = sorted(active)
        self.report.overridden.sort()
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
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("skillspec must be a YAML object")
                spec = SkillSpec.model_validate(raw)
                records[name] = _SkillSpecRecord(name=name, spec=spec, path=path, source=source)
            except Exception as exc:
                self.report.invalid.append(f"{source}:{name} ({exc})")
        return records
