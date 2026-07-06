"""Ingestion guardrails (§6.3) — pure validation, no GCP calls."""

import pytest

from app.agents.ingestion import IngestionError, decode_document


def test_rejects_unsupported_mime():
    with pytest.raises(IngestionError, match="Unsupported file type"):
        decode_document(b"hello", "text/plain")


def test_rejects_docx_masquerade():
    with pytest.raises(IngestionError):
        decode_document(
            b"PK\x03\x04",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )


def test_rejects_oversized_file():
    with pytest.raises(IngestionError, match="too large"):
        decode_document(b"x" * (10 * 1024 * 1024 + 1), "image/jpeg")


def test_rejects_empty_file():
    with pytest.raises(IngestionError, match="empty"):
        decode_document(b"", "image/png")


def test_accepts_valid_small_jpeg():
    decode_document(b"\xff\xd8\xff\xe0 fake jpeg bytes", "image/jpeg")  # no raise
