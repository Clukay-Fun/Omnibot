from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.skill_runtime.document_extractor import (
    ExtractionQualityError,
    extract_fields,
    load_extract_templates,
)


def test_load_templates_with_workspace_override(tmp_path: Path) -> None:
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir(parents=True)
    (extract_dir / "invoice.yaml").write_text(
        "\n".join(
            [
                "id: invoice_workspace",
                "document_type: invoice",
                "fields:",
                "  - name: invoice_number",
                "    required: true",
                "    patterns:",
                "      - 'Invoice ID[: ]+([A-Z0-9-]+)'",
            ]
        ),
        encoding="utf-8",
    )

    templates = load_extract_templates(tmp_path)

    assert "invoice" in templates
    assert templates["invoice"].template_id == "invoice_workspace"


def test_extract_fields_success() -> None:
    text = """
    Invoice Number: INV-2026-009
    Total Amount: $2048.00
    """
    template = load_extract_templates()["invoice"]

    result = extract_fields(text, template)

    assert result.fields["invoice_number"] == "INV-2026-009"
    assert result.fields["total_amount"] == "$2048.00"
    assert result.confidence == 1.0


def test_extract_fields_low_quality_error() -> None:
    text = "Invoice Number: INV-2026-010"
    template = load_extract_templates()["invoice"]

    with pytest.raises(ExtractionQualityError) as exc:
        extract_fields(text, template)

    assert "Low-quality extraction" in str(exc.value)
    assert "total_amount" in str(exc.value)
