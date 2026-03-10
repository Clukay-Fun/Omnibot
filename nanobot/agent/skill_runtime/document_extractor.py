"""Compatibility shim for document extractor imports."""

from nanobot.agent.documents.document_extractor import (
    ExtractionError,
    ExtractionQualityError,
    ExtractFieldRule,
    ExtractionResult,
    ExtractTemplate,
    extract_fields,
    load_extract_templates,
)

__all__ = [
    "ExtractionError",
    "ExtractionQualityError",
    "ExtractFieldRule",
    "ExtractionResult",
    "ExtractTemplate",
    "extract_fields",
    "load_extract_templates",
]
