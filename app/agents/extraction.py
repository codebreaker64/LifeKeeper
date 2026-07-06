"""
Agent 2/4 — EXTRACTION AGENT (Design Doc §2.1, §4.2)

Responsibility: turn OCR text into a typed, structured record:
    {doc_type, expiry_date, owner_name, reference_number, issuer, confidence}

Skills implemented:
    classify_document / extract_dates / extract_fields — one structured
        Gemini call using JSON response mode (single call = fewer failure
        modes and lower latency than three chained calls)
    compute_next_check_at — urgency windows per doc type (§4.2 table)

The LLM is constrained by a JSON schema, and the output is *re-validated in
Python* before anything touches Firestore — never trust model output blindly
(rubric: security/guardrails).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..config import get_settings

log = logging.getLogger("lifekeeper.extraction")


class ExtractionError(Exception):
    """OCR text yielded no usable expiry date. Deliberately NOT a ValueError:
    json.JSONDecodeError subclasses ValueError, and a Gemini output glitch
    must not be reported to the user as 'no expiry date found'."""


DOC_TYPES = ["passport", "licence", "insurance", "warranty", "membership", "other"]

# Doc types where an issuing *country* is a meaningful concept and drives the
# renewal playbook (a passport is renewed with the issuing government). For
# insurance/warranty/membership the issuer — not a country — is what matters,
# so we never ask for or store a country on those.
COUNTRY_RELEVANT_TYPES = frozenset({"passport", "licence"})


def country_is_relevant(doc_type: str) -> bool:
    return doc_type in COUNTRY_RELEVANT_TYPES

# Urgency windows in days before expiry (Design Doc §4.2 table).
# Tiers: early_warning → final_reminder → overdue (on expiry).
URGENCY_WINDOWS: dict[str, dict[str, int]] = {
    "passport":   {"early_warning": 90, "final_reminder": 30},
    "licence":    {"early_warning": 60, "final_reminder": 14},
    "insurance":  {"early_warning": 45, "final_reminder": 14},
    "warranty":   {"early_warning": 30, "final_reminder": 7},
    "membership": {"early_warning": 30, "final_reminder": 7},
    "other":      {"early_warning": 30, "final_reminder": 7},
}

_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": DOC_TYPES},
        "expiry_date": {
            "type": "string",
            "description": "ISO 8601 date (YYYY-MM-DD). The EXPIRY/valid-until "
                           "date, not the issue date. null if none found.",
        },
        "owner_name": {"type": "string"},
        "reference_number": {
            "type": "string",
            "description": "Passport no., policy no., licence no., etc.",
        },
        "issuer": {"type": "string", "description": "e.g. HMPO, DVLA, Aviva"},
        "issuing_country": {
            "type": "string",
            "description": "Country that ISSUED the document, as an ISO "
                           "3166-1 alpha-2 code (SG, GB, US, MY...). Infer "
                           "from the issuer, country field, MRZ, or language. "
                           "Omit if genuinely unclear.",
        },
    },
    "required": ["doc_type"],
}

_PROMPT = """You are the Extraction Agent in LifeKeeper, a document-renewal assistant.
Given OCR text from a personal document, extract a structured record.

Rules:
- Dates may appear in DD/MM/YYYY, MM-DD-YYYY, "14 MAR 2027", etc. Normalise to YYYY-MM-DD.
- If both issue and expiry dates appear, expiry is the LATER one, usually labelled
  "Date of expiry", "Valid until", "Expires", "Renewal date".
- If genuinely no expiry date exists, return null for expiry_date.
- issuing_country is the country that issued the document (two-letter ISO
  code). A UK passport is GB; an ICA-issued document is SG. Renewal happens
  with the issuing country regardless of where the owner lives.
- Do not invent values. Omit fields you cannot find.

OCR TEXT:
---
{text}
---"""

# Country names/codes we can map to ISO 3166-1 alpha-2 without a lookup
# service. Used for both model output and typed user answers ("Singapore").
COUNTRY_ALIASES: dict[str, str] = {
    "sg": "SG", "singapore": "SG",
    "gb": "GB", "uk": "GB", "united kingdom": "GB", "britain": "GB",
    "great britain": "GB", "england": "GB",
    "us": "US", "usa": "US", "united states": "US", "america": "US",
    "united states of america": "US",
    "my": "MY", "malaysia": "MY",
    "au": "AU", "australia": "AU",
    "nz": "NZ", "new zealand": "NZ",
    "in": "IN", "india": "IN",
    "cn": "CN", "china": "CN",
    "hk": "HK", "hong kong": "HK",
    "id": "ID", "indonesia": "ID",
    "ca": "CA", "canada": "CA",
    "ie": "IE", "ireland": "IE",
    "de": "DE", "germany": "DE",
    "fr": "FR", "france": "FR",
    "jp": "JP", "japan": "JP",
    "kr": "KR", "south korea": "KR", "korea": "KR",
    "ph": "PH", "philippines": "PH",
    "th": "TH", "thailand": "TH",
    "vn": "VN", "vietnam": "VN",
}


def normalize_country(raw: Optional[str]) -> Optional[str]:
    """Best-effort country → ISO alpha-2 code. Unknown countries round-trip
    as a cleaned name (they just won't match a renewal playbook URL)."""
    if not raw:
        return None
    cleaned = raw.strip().strip(".").lower()
    if not cleaned:
        return None
    if cleaned in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[cleaned]
    if len(cleaned) == 2 and cleaned.isalpha():
        return cleaned.upper()
    return raw.strip().title()


def extract_record(ocr_text: str) -> dict[str, Any]:
    """One structured LLM call → validated dict. Raises ExtractionError on
    unusable output so the caller can ask the user instead of guessing."""
    from google import genai

    s = get_settings()
    client = genai.Client(api_key=s.gemini_api_key) if s.gemini_api_key else genai.Client(
        vertexai=True, project=s.gcp_project_id, location="us-central1"
    )
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=_PROMPT.format(text=ocr_text[:15000]),  # cap prompt size
        config={
            "response_mime_type": "application/json",
            "response_schema": _EXTRACTION_SCHEMA,
        },
    )
    raw = json.loads(resp.text)

    # ---- Re-validate in Python (never trust the model) ----
    doc_type = raw.get("doc_type", "other")
    if doc_type not in DOC_TYPES:
        doc_type = "other"

    expiry: Optional[datetime] = None
    if raw.get("expiry_date"):
        try:
            expiry = datetime.fromisoformat(raw["expiry_date"]).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            log.warning("Model returned unparseable date: %r", raw["expiry_date"])

    if expiry is None:
        raise ExtractionError("no_expiry_date")

    return {
        "doc_type": doc_type,
        "expiry_date": expiry,
        "owner_name": raw.get("owner_name"),
        "reference_number": raw.get("reference_number"),
        "issuer": raw.get("issuer"),
        "issuing_country": normalize_country(raw.get("issuing_country")),
    }


def compute_next_check_at(expiry_date: datetime, doc_type: str) -> datetime:
    """Skill `compute_next_check_at` (§4.2): the next moment the scheduler
    needs to look at this record — the earliest tier boundary still ahead.
    If all boundaries have passed, check immediately (record is overdue)."""
    windows = URGENCY_WINDOWS.get(doc_type, URGENCY_WINDOWS["other"])
    now = datetime.now(timezone.utc)
    boundaries = sorted([
        expiry_date - timedelta(days=windows["early_warning"]),
        expiry_date - timedelta(days=windows["final_reminder"]),
        expiry_date,  # overdue boundary
    ])
    for b in boundaries:
        if b > now:
            return b
    return now  # already past expiry — scheduler should pick it up now
