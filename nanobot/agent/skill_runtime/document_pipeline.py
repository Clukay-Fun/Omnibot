"""Document pipeline entrypoint for skillspec execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from nanobot.agent.skill_runtime.document_classifier import DocumentClassifier
from nanobot.agent.skill_runtime.document_extractor import (
    ExtractionError,
    ExtractionQualityError,
    ExtractTemplate,
    extract_fields,
    load_extract_templates,
)
from nanobot.agent.skill_runtime.mineru_client import (
    SUPPORTED_DOCUMENT_EXTENSIONS,
    MinerUClient,
    MinerUClientError,
    MinerUTimeoutError,
)
from nanobot.config.loader import load_config


@dataclass(slots=True)
class DocumentPipelineItemResult:
    """Single document processing outcome."""

    path: str
    document_type: str
    classification_confidence: float
    extracted_fields: dict[str, str]
    extraction_confidence: float
    errors: list[str]
    template_id: str | None = None
    status: str = "ready"
    write_ready: bool = False


async def process_document(
    paths: list[str],
    skill_id: str,
    user_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Process documents through MinerU + classification + extraction.

    API intentionally stable for later Phase C executor wiring.
    """
    _ = user_context
    if not paths:
        return {
            "skill_id": skill_id,
            "results": [],
            "errors": ["No document paths provided"],
        }

    config = load_config()
    templates = load_extract_templates(config.workspace_path)
    classifier = DocumentClassifier()
    mineru = MinerUClient(config.tools.mineru)

    results: list[DocumentPipelineItemResult] = []
    errors: list[str] = []
    error_details: list[dict[str, str]] = []

    for raw_path in paths:
        path = Path(raw_path).expanduser()
        item_errors: list[str] = []
        if path.suffix.lower() not in SUPPORTED_DOCUMENT_EXTENSIONS:
            allowed = ", ".join(sorted(SUPPORTED_DOCUMENT_EXTENSIONS))
            message = (
                f"Unsupported file format '{path.suffix or '<none>'}' for {path.name}. Allowed formats: {allowed}"
            )
            errors.append(f"[UNSUPPORTED_FORMAT] {message}")
            error_details.append({"code": "UNSUPPORTED_FORMAT", "path": str(path), "message": message})
            continue
        if not path.exists() or not path.is_file():
            message = f"Document file not found: {path}"
            errors.append(f"[FILE_NOT_FOUND] {message}")
            error_details.append({"code": "FILE_NOT_FOUND", "path": str(path), "message": message})
            continue

        if not config.tools.mineru.enabled:
            message = "MinerU integration is disabled in config.tools.mineru.enabled"
            errors.append(message)
            error_details.append({"code": "MINERU_DISABLED", "path": str(path), "message": message})
            break

        try:
            payload = await mineru.submit_and_wait(path)
            text = _extract_text(payload)
            classification = classifier.classify(text=text, filename=path.name)
            template = _select_template(templates, classification.document_type)
            if not template:
                message = f"No extraction template found for classified type '{classification.document_type}'"
                item_errors.append(message)
                errors.append(f"[TEMPLATE_MISSING] {path.name}: {message}")
                error_details.append({"code": "TEMPLATE_MISSING", "path": str(path), "message": message})
                results.append(
                    DocumentPipelineItemResult(
                        path=str(path),
                        document_type=classification.document_type,
                        classification_confidence=classification.confidence,
                        extracted_fields={},
                        extraction_confidence=0.0,
                        errors=item_errors,
                        status="template_missing",
                        write_ready=False,
                    )
                )
                continue

            try:
                extraction = extract_fields(text, template)
            except ExtractionQualityError as exc:
                message = str(exc)
                item_errors.append(message)
                errors.append(f"[LOW_QUALITY_EXTRACTION] {path.name}: {message}")
                error_details.append({"code": "LOW_QUALITY_EXTRACTION", "path": str(path), "message": message})
                results.append(
                    DocumentPipelineItemResult(
                        path=str(path),
                        document_type=classification.document_type,
                        classification_confidence=classification.confidence,
                        extracted_fields={},
                        extraction_confidence=0.0,
                        errors=item_errors,
                        template_id=template.template_id,
                        status="low_quality",
                        write_ready=False,
                    )
                )
                continue

            results.append(
                DocumentPipelineItemResult(
                    path=str(path),
                    document_type=classification.document_type,
                    classification_confidence=classification.confidence,
                    extracted_fields=extraction.fields,
                    extraction_confidence=extraction.confidence,
                    errors=item_errors,
                    template_id=extraction.template_id,
                    status="ready",
                    write_ready=bool(extraction.fields),
                )
            )
        except MinerUTimeoutError as exc:
            message = str(exc)
            errors.append(f"[API_TIMEOUT] {path.name}: {message}")
            error_details.append({"code": "API_TIMEOUT", "path": str(path), "message": message})
        except MinerUClientError as exc:
            message = str(exc)
            errors.append(f"[API_ERROR] {path.name}: {message}")
            error_details.append({"code": "API_ERROR", "path": str(path), "message": message})
        except ExtractionError as exc:
            message = str(exc)
            errors.append(f"[LOW_QUALITY_EXTRACTION] {path.name}: {message}")
            error_details.append({"code": "LOW_QUALITY_EXTRACTION", "path": str(path), "message": message})
            results.append(
                DocumentPipelineItemResult(
                    path=str(path),
                    document_type="unknown",
                    classification_confidence=0.0,
                    extracted_fields={},
                    extraction_confidence=0.0,
                    errors=[message],
                    status="low_quality",
                    write_ready=False,
                )
            )

    return {
        "skill_id": skill_id,
        "results": [asdict(item) for item in results],
        "errors": errors,
        "error_details": error_details,
    }


def _extract_text(payload: dict[str, Any]) -> str:
    for key in ("text", "markdown", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    if isinstance(payload.get("result"), dict):
        result = payload["result"]
        for key in ("text", "markdown", "content"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
    raise ExtractionError("Low-quality extraction: MinerU result does not contain usable text")


def _select_template(templates: dict[str, ExtractTemplate], document_type: str) -> ExtractTemplate | None:
    if document_type in templates:
        return templates[document_type]
    return templates.get("default")
