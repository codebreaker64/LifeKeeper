"""
Agent 1/4 — INGESTION AGENT (Design Doc §2.1, §4.1)

Responsibility: turn a raw uploaded file into clean text + a confidence score.
Everything downstream (Extraction, Scheduler, Renewal) is source-agnostic —
whether the file arrived via Telegram or any future channel, it lands here.

Skills implemented:
    decode_document()   — MIME whitelist + 10MB cap (guardrails §6.3)
    run_document_ai()   — Google Document AI OCR (primary)
    fallback_vision()   — Gemini multimodal fallback when Document AI is
                          unavailable or returns very low confidence
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from google.api_core.client_options import ClientOptions
from google.cloud import documentai

from ..config import get_settings

log = logging.getLogger("lifekeeper.ingestion")


class IngestionError(Exception):
    """Raised for user-correctable problems (bad file type, too large).
    The webhook layer converts these into friendly Telegram messages."""


@dataclass
class OCRResult:
    text: str
    confidence: float      # 0.0–1.0, propagated into the Firestore record
    engine: str            # "document_ai" | "gemini_vision"


# ------------------------------------------------------------ validation ---

def decode_document(data: bytes, mime_type: str) -> None:
    """Guardrails from Design Doc §6.3 — enforced BEFORE any paid API call."""
    s = get_settings()
    if mime_type not in s.allowed_mime_types:
        raise IngestionError(
            "Unsupported file type. Please send a PDF, JPEG, or PNG."
        )
    if len(data) > s.max_file_bytes:
        raise IngestionError("File too large — the limit is 10MB.")
    if len(data) == 0:
        raise IngestionError("The file appears to be empty.")


# ------------------------------------------------------------ document ai ---

def run_document_ai(data: bytes, mime_type: str) -> OCRResult:
    """Primary OCR path. Document AI handles skew, glare and low contrast on
    phone photos far better than plain vision prompting (Design Doc §2.3)."""
    s = get_settings()
    client = documentai.DocumentProcessorServiceClient(
        client_options=ClientOptions(
            api_endpoint=f"{s.gcp_location}-documentai.googleapis.com"
        )
    )
    name = client.processor_path(s.gcp_project_id, s.gcp_location, s.docai_processor_id)

    request = documentai.ProcessRequest(
        name=name,
        raw_document=documentai.RawDocument(content=data, mime_type=mime_type),
    )
    doc = client.process_document(request=request).document

    # Document AI reports confidence per detected page; average them.
    confidences = [p.layout.confidence for p in doc.pages if p.layout]
    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return OCRResult(text=doc.text or "", confidence=confidence, engine="document_ai")


# ------------------------------------------------------- gemini fallback ---

def fallback_vision(data: bytes, mime_type: str) -> OCRResult:
    """Fallback when Document AI errors out or reads almost nothing.
    Uses Gemini multimodal to transcribe the document verbatim. Confidence is
    set just below the gate so the user is *always* asked to confirm —
    a fallback path should never silently persist (guardrail §6.3)."""
    from google import genai  # local import: keeps cold-start fast on the happy path

    s = get_settings()
    client = genai.Client(api_key=s.gemini_api_key) if s.gemini_api_key else genai.Client(
        vertexai=True, project=s.gcp_project_id, location="us-central1"
    )
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            genai.types.Part.from_bytes(data=data, mime_type=mime_type),
            "Transcribe ALL text visible in this document verbatim. "
            "Preserve dates, reference numbers, and names exactly as written.",
        ],
    )
    threshold = get_settings().confidence_threshold
    return OCRResult(
        text=resp.text or "",
        confidence=max(threshold - 0.05, 0.0),  # force the confirm gate
        engine="gemini_vision",
    )


# ------------------------------------------------------------ entrypoint ---

def ingest(data: bytes, mime_type: str) -> OCRResult:
    """Full ingestion pipeline: validate → Document AI → fallback."""
    decode_document(data, mime_type)
    try:
        result = run_document_ai(data, mime_type)
        # If OCR produced essentially nothing, the photo may be too poor for
        # Document AI's OCR processor — give Gemini vision a try.
        if len(result.text.strip()) < 20:
            log.warning("Document AI returned <20 chars; trying Gemini fallback")
            return fallback_vision(data, mime_type)
        return result
    except IngestionError:
        raise
    except Exception:
        log.exception("Document AI failed; using Gemini vision fallback")
        return fallback_vision(data, mime_type)
