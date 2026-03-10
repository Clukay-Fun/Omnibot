"""Compatibility shim for MinerU client imports."""

from nanobot.agent.documents.mineru_client import (
    SUPPORTED_DOCUMENT_EXTENSIONS,
    MinerUClient,
    MinerUClientError,
    MinerUTimeoutError,
)

__all__ = [
    "SUPPORTED_DOCUMENT_EXTENSIONS",
    "MinerUClient",
    "MinerUClientError",
    "MinerUTimeoutError",
]
