from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.agent.skill_runtime.document_extractor import (
    ExtractionQualityError,
    extract_fields,
    load_extract_templates,
)
from nanobot.agent.skill_runtime.document_pipeline import process_document


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


def test_load_templates_includes_packaged_builtin_extract_assets() -> None:
    templates = load_extract_templates()

    assert templates["invoice"].template_id == "invoice_minimal"
    assert templates["contract"].template_id == "contract_minimal"


def test_load_templates_prefers_workspace_extract_over_skillspec_extract(tmp_path: Path) -> None:
    skillspec_extract = tmp_path / "skillspec" / "extract"
    workspace_extract = tmp_path / "extract"
    skillspec_extract.mkdir(parents=True)
    workspace_extract.mkdir(parents=True)

    (skillspec_extract / "invoice.yaml").write_text(
        "\n".join(
            [
                "id: invoice_skillspec",
                "document_type: invoice",
                "fields:",
                "  - name: invoice_number",
                "    required: true",
                "    patterns:",
                "      - 'Invoice Number[: ]+([A-Z0-9-]+)'",
            ]
        ),
        encoding="utf-8",
    )
    (workspace_extract / "invoice.yaml").write_text(
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

    assert templates["invoice"].template_id == "invoice_workspace"


def test_load_templates_ignores_invalid_workspace_override(tmp_path: Path) -> None:
    builtin_invoice_id = load_extract_templates()["invoice"].template_id
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir(parents=True)
    (extract_dir / "invoice.yaml").write_text("id: broken", encoding="utf-8")

    templates = load_extract_templates(tmp_path)

    assert templates["invoice"].template_id == builtin_invoice_id


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


@pytest.mark.asyncio
async def test_process_document_reports_low_quality_error(monkeypatch, tmp_path: Path) -> None:
    document = tmp_path / "invoice.pdf"
    document.write_bytes(b"pdf")

    class _MinerU:
        def __init__(self, config):
            _ = config

        async def submit_and_wait(self, path: Path) -> dict:
            _ = path
            return {"result": {"text": "Invoice Number: INV-1"}}

    cfg = SimpleNamespace(
        workspace_path=tmp_path,
        tools=SimpleNamespace(mineru=SimpleNamespace(enabled=True)),
    )
    monkeypatch.setattr("nanobot.agent.skill_runtime.document_pipeline.load_config", lambda: cfg)
    monkeypatch.setattr("nanobot.agent.skill_runtime.document_pipeline.MinerUClient", _MinerU)

    payload = await process_document([str(document)], skill_id="doc_recognize", user_context=None)

    assert len(payload["results"]) == 1
    assert payload["results"][0]["status"] == "low_quality"
    assert payload["results"][0]["write_ready"] is False
    assert payload["errors"]
    assert payload["errors"][0].startswith("[LOW_QUALITY_EXTRACTION]")
    assert payload["error_details"][0]["code"] == "LOW_QUALITY_EXTRACTION"


@pytest.mark.asyncio
async def test_process_document_template_missing_has_structured_result(monkeypatch, tmp_path: Path) -> None:
    document = tmp_path / "contract.pdf"
    document.write_bytes(b"pdf")

    class _MinerU:
        def __init__(self, config):
            _ = config

        async def submit_and_wait(self, path: Path) -> dict:
            _ = path
            return {"result": {"text": "This is a contract document."}}

    class _Classifier:
        def classify(self, text: str, filename: str):
            _ = (text, filename)
            return SimpleNamespace(document_type="custom_contract", confidence=0.95)

    cfg = SimpleNamespace(
        workspace_path=tmp_path,
        tools=SimpleNamespace(mineru=SimpleNamespace(enabled=True)),
    )
    monkeypatch.setattr("nanobot.agent.skill_runtime.document_pipeline.load_config", lambda: cfg)
    monkeypatch.setattr("nanobot.agent.skill_runtime.document_pipeline.MinerUClient", _MinerU)
    monkeypatch.setattr("nanobot.agent.skill_runtime.document_pipeline.DocumentClassifier", _Classifier)
    monkeypatch.setattr("nanobot.agent.skill_runtime.document_pipeline.load_extract_templates", lambda _: {})

    payload = await process_document([str(document)], skill_id="doc_recognize", user_context=None)

    assert len(payload["results"]) == 1
    assert payload["results"][0]["status"] == "template_missing"
    assert payload["results"][0]["write_ready"] is False
    assert payload["results"][0]["document_type"] == "custom_contract"
    assert payload["errors"][0].startswith("[TEMPLATE_MISSING]")
    assert payload["error_details"][0]["code"] == "TEMPLATE_MISSING"


@pytest.mark.asyncio
async def test_process_document_reports_missing_file_and_unsupported_format(monkeypatch, tmp_path: Path) -> None:
    missing_pdf = tmp_path / "missing.pdf"
    unsupported = tmp_path / "archive.zip"
    unsupported.write_bytes(b"zip")

    cfg = SimpleNamespace(
        workspace_path=tmp_path,
        tools=SimpleNamespace(mineru=SimpleNamespace(enabled=False)),
    )
    monkeypatch.setattr("nanobot.agent.skill_runtime.document_pipeline.load_config", lambda: cfg)

    payload = await process_document([str(unsupported), str(missing_pdf)], skill_id="doc_recognize", user_context=None)

    assert any(err.startswith("[UNSUPPORTED_FORMAT]") for err in payload["errors"])
    assert any(err.startswith("[FILE_NOT_FOUND]") for err in payload["errors"])
