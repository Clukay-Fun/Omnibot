"""Template-based field extractor for parsed document text."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml


class ExtractionError(RuntimeError):
    """Base extraction error."""


class ExtractionQualityError(ExtractionError):
    """Raised when extraction quality is below acceptable threshold."""


@dataclass(slots=True)
class ExtractFieldRule:
    name: str
    patterns: list[str]
    required: bool = False


@dataclass(slots=True)
class ExtractTemplate:
    template_id: str
    document_type: str
    fields: list[ExtractFieldRule] = field(default_factory=list)


@dataclass(slots=True)
class ExtractionResult:
    template_id: str
    document_type: str
    fields: dict[str, str]
    missing_required_fields: list[str]
    confidence: float


def load_extract_templates(workspace_root: Path | None = None) -> dict[str, ExtractTemplate]:
    """Load builtin templates and merge workspace overrides."""
    templates: dict[str, ExtractTemplate] = {}

    builtin_dir = files("nanobot") / "skills" / "extract_templates"
    for resource in builtin_dir.iterdir():
        if resource.name.endswith((".yaml", ".yml")):
            data = yaml.safe_load(resource.read_text(encoding="utf-8"))
            tpl = _parse_template(data, source=resource.name)
            templates[tpl.document_type] = tpl

    if workspace_root:
        for workspace_dir in _workspace_template_dirs(workspace_root):
            if not workspace_dir.exists():
                continue
            for path in sorted(list(workspace_dir.glob("*.yaml")) + list(workspace_dir.glob("*.yml"))):
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8"))
                    tpl = _parse_template(data, source=str(path))
                except (yaml.YAMLError, ExtractionError):
                    continue
                templates[tpl.document_type] = tpl

    return templates


def extract_fields(text: str, template: ExtractTemplate) -> ExtractionResult:
    """Extract fields by regex pattern definitions from template."""
    values: dict[str, str] = {}
    missing_required: list[str] = []

    for field_rule in template.fields:
        matched = ""
        for pattern in field_rule.patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
            if match:
                matched = _normalize_match(match)
                if matched:
                    break
        if matched:
            values[field_rule.name] = matched
        elif field_rule.required:
            missing_required.append(field_rule.name)

    required_total = sum(1 for f in template.fields if f.required)
    confidence = _compute_confidence(values=values, missing_required=missing_required, required_total=required_total)
    if required_total and missing_required:
        missing_text = ", ".join(missing_required)
        raise ExtractionQualityError(
            "Low-quality extraction: missing required fields "
            f"[{missing_text}] for template '{template.template_id}'"
        )

    return ExtractionResult(
        template_id=template.template_id,
        document_type=template.document_type,
        fields=values,
        missing_required_fields=missing_required,
        confidence=confidence,
    )


def _parse_template(data: dict[str, Any], source: str) -> ExtractTemplate:
    if not isinstance(data, dict):
        raise ExtractionError(f"Invalid extract template in {source}: YAML object required")

    template_id = str(data.get("id") or "").strip()
    document_type = str(data.get("document_type") or "").strip()
    raw_fields = data.get("fields")
    if not template_id or not document_type:
        raise ExtractionError(f"Invalid extract template in {source}: id and document_type are required")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ExtractionError(f"Invalid extract template in {source}: fields must be a non-empty list")

    fields: list[ExtractFieldRule] = []
    for idx, item in enumerate(raw_fields):
        if not isinstance(item, dict):
            raise ExtractionError(f"Invalid field at index {idx} in {source}: object required")
        name = str(item.get("name") or "").strip()
        patterns = item.get("patterns")
        if not name or not isinstance(patterns, list) or not patterns:
            raise ExtractionError(
                f"Invalid field definition '{name or idx}' in {source}: "
                "name and non-empty patterns are required"
            )
        fields.append(
            ExtractFieldRule(
                name=name,
                patterns=[str(pattern) for pattern in patterns],
                required=bool(item.get("required", False)),
            )
        )

    return ExtractTemplate(template_id=template_id, document_type=document_type, fields=fields)


def _normalize_match(match: re.Match[str]) -> str:
    value = match.group(1) if match.lastindex else match.group(0)
    return " ".join(value.strip().split())


def _compute_confidence(values: dict[str, str], missing_required: list[str], required_total: int) -> float:
    if required_total == 0:
        return 1.0 if values else 0.0
    hit = required_total - len(missing_required)
    return max(0.0, min(1.0, hit / required_total))


def _workspace_template_dirs(workspace_root: Path) -> list[Path]:
    return [
        workspace_root / "skillspec" / "extract",
        workspace_root / "extract",
    ]
