"""Runtime text/template loader with workspace overrides."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def _deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = _deep_merge(merged.get(key), value)
        return merged
    return override if override is not None else base


def _safe_load_yaml_text(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _safe_load_json_text(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _load_workspace_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return _safe_load_yaml_text(_read_text(path))


def _load_workspace_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return _safe_load_json_text(_read_text(path))


def _load_bundled_text(rel_path: str) -> str:
    try:
        from importlib.resources import files

        resource = files("nanobot") / "templates" / "workspace" / rel_path
        return resource.read_text(encoding="utf-8")
    except Exception:
        return ""


@dataclass(slots=True)
class RuntimeTextCatalog:
    prompts: dict[str, dict[str, Any]]
    routing: dict[str, dict[str, Any]]
    templates: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, workspace: Path | None) -> "RuntimeTextCatalog":
        prompt_files = (
            "smalltalk",
            "onboarding",
            "preference",
            "help",
            "pagination",
            "progress",
        )
        routing_files = (
            "smalltalk_triggers",
            "preference_triggers",
            "pagination_triggers",
            "domain_hints",
        )
        template_files = (
            "onboarding_form",
            "card_confirm",
            "card_case",
            "card_contract",
            "card_overview",
            "card_summary",
        )

        prompts: dict[str, dict[str, Any]] = {}
        for name in prompt_files:
            bundled = _safe_load_yaml_text(_load_bundled_text(f"prompts/{name}.yaml"))
            override = _load_workspace_yaml(workspace / "prompts" / f"{name}.yaml") if workspace else {}
            prompts[name] = _deep_merge(bundled, override)

        routing: dict[str, dict[str, Any]] = {}
        for name in routing_files:
            bundled = _safe_load_yaml_text(_load_bundled_text(f"routing/{name}.yaml"))
            override = _load_workspace_yaml(workspace / "routing" / f"{name}.yaml") if workspace else {}
            routing[name] = _deep_merge(bundled, override)

        templates: dict[str, dict[str, Any]] = {}
        for name in template_files:
            bundled = _safe_load_json_text(_load_bundled_text(f"templates/{name}.json"))
            override = _load_workspace_json(workspace / "templates" / f"{name}.json") if workspace else {}
            templates[name] = _deep_merge(bundled, override)

        return cls(prompts=prompts, routing=routing, templates=templates)

    def prompt_text(self, group: str, key: str, default: str = "") -> str:
        value = self.prompts.get(group, {}).get(key)
        return str(value) if isinstance(value, str) else default

    def prompt_lines(self, group: str, key: str, default: list[str] | None = None) -> list[str]:
        value = self.prompts.get(group, {}).get(key)
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str):
            return [line for line in value.splitlines() if line.strip()]
        return list(default or [])

    def routing_list(self, group: str, key: str, default: list[str] | None = None) -> list[str]:
        value = self.routing.get(group, {}).get(key)
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return list(default or [])

    def template(self, name: str) -> dict[str, Any]:
        raw = self.templates.get(name)
        return dict(raw) if isinstance(raw, dict) else {}
