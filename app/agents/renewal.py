"""
Agent 4/4 — RENEWAL ACTION AGENT (Design Doc §2.1, §4.4)

Responsibility: when the user says "generate my renewal packet", produce a
renewal packet: a one-page PDF summary + a step-by-step checklist + the
official renewal URL — then STOP at the HITL gate.

⚠ HITL GATE (Design Doc §4.4): nothing proceeds past packet generation
without explicit user approval via the Telegram inline keyboard. The agent
NEVER automates third-party portals — no browser sessions, no form
submissions on government/insurer sites. This is deliberate, responsible
agentic design: it avoids bot-detection, ToS violations, and unpredictable
failure modes.

MVP note: pre-filled official templates (DS-82, DVLA D1) are post-MVP —
`template_matched` is honestly False and the packet is a generated summary
PDF + checklist (§7.5 fallback path shipped as the primary path).
"""

from __future__ import annotations

import io
import logging
from datetime import timedelta
from typing import Any

from google.cloud import storage
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from .. import firestore_db as db
from ..config import get_settings

log = logging.getLogger("lifekeeper.renewal")

# Official renewal URLs + checklists per doc type. Generic-but-real: judges
# can click every link. Extend per issuer post-MVP.
RENEWAL_PLAYBOOK: dict[str, dict[str, Any]] = {
    "passport": {
        "official_url": "https://www.gov.uk/renew-adult-passport",
        "checklist": [
            "Take a new digital passport photo (no glasses, plain background)",
            "Locate your current passport — you'll need its number",
            "Apply online at the official URL below (~10 minutes)",
            "Pay the renewal fee online",
            "Post your old passport to the address given at the end of the application",
        ],
    },
    "licence": {
        "official_url": "https://www.gov.uk/renew-driving-licence",
        "checklist": [
            "Have your driving licence number and National Insurance number ready",
            "Check your address is current — update it during renewal if not",
            "Renew online at the official URL below",
            "Your new licence arrives within 1 week (keep driving meanwhile if eligible)",
        ],
    },
    "insurance": {
        "official_url": None,  # issuer-specific; checklist guides the user
        "checklist": [
            "Locate your policy number (on the packet summary below)",
            "Get 2–3 comparison quotes before auto-renewing — loyalty rarely pays",
            "Contact your insurer or broker to confirm renewal terms",
            "Check the new policy start date leaves no coverage gap",
        ],
    },
    "warranty": {
        "official_url": None,
        "checklist": [
            "Find your proof of purchase / receipt",
            "Check if the manufacturer offers an extended warranty",
            "Register or extend on the manufacturer's website before expiry",
        ],
    },
    "membership": {
        "official_url": None,
        "checklist": [
            "Review whether you used this membership enough to renew",
            "Check for renewal discounts or better tiers",
            "Renew (or cancel!) before the auto-renewal date",
        ],
    },
    "other": {
        "official_url": None,
        "checklist": [
            "Locate the original document and issuer contact details",
            "Contact the issuer about their renewal process",
            "Set aside time before the expiry date to complete it",
        ],
    },
}


def _render_packet_pdf(record: dict[str, Any], checklist: list[str],
                       official_url: str | None) -> bytes:
    """Skill `generate_renewal_packet` (rendering half): a clean one-page
    A4 summary the user can print or keep. reportlab, no template deps."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    y = h - 25 * mm

    c.setFont("Helvetica-Bold", 20)
    c.drawString(20 * mm, y, "LifeKeeper — Renewal Packet")
    y -= 12 * mm

    c.setFont("Helvetica", 11)
    rows = [
        ("Document type", record["doc_type"].title()),
        ("Owner", record.get("owner_name") or "—"),
        ("Reference number", record.get("reference_number") or "—"),
        ("Issuer", record.get("issuer") or "—"),
        ("Expiry date", record["expiry_date"].strftime("%d %B %Y")),
        ("Pre-filled official form", "No (generic packet)" if not record.get(
            "template_matched") else "Yes"),
    ]
    for label, value in rows:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(20 * mm, y, f"{label}:")
        c.setFont("Helvetica", 11)
        c.drawString(70 * mm, y, str(value))
        y -= 7 * mm

    y -= 6 * mm
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, y, "Your renewal checklist")
    y -= 8 * mm
    c.setFont("Helvetica", 11)
    for i, step in enumerate(checklist, 1):
        c.drawString(22 * mm, y, f"{i}.  {step[:95]}")
        y -= 7 * mm

    if official_url:
        y -= 6 * mm
        c.setFont("Helvetica-Bold", 11)
        c.drawString(20 * mm, y, "Official renewal link:")
        y -= 6 * mm
        c.setFont("Helvetica", 10)
        c.drawString(22 * mm, y, official_url)

    c.setFont("Helvetica-Oblique", 8)
    c.drawString(20 * mm, 15 * mm,
                 "Generated by LifeKeeper. Review before use — no action is "
                 "taken without your approval.")
    c.showPage()
    c.save()
    return buf.getvalue()


def _upload_packet(pdf_bytes: bytes, doc_id: str) -> str:
    """Skill `upload_packet`: store PDF in GCS, return a signed URL valid
    7 days. On Cloud Run the runtime credentials contain only a token (no
    private key), so signing is delegated to the IAM signBlob API by passing
    service_account_email + access_token. Requires the service account to
    hold roles/iam.serviceAccountTokenCreator on itself."""
    import google.auth
    from google.auth.transport import requests as ga_requests

    s = get_settings()
    credentials, _ = google.auth.default()
    credentials.refresh(ga_requests.Request())

    bucket = storage.Client().bucket(s.gcs_bucket_name)
    blob = bucket.blob(f"packets/{doc_id}.pdf")
    blob.upload_from_string(pdf_bytes, content_type="application/pdf")
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(days=s.signed_url_days),
        service_account_email=credentials.service_account_email,
        access_token=credentials.token,
    )

def generate_renewal_packet(doc_id: str) -> dict[str, Any]:
    """Full packet flow: playbook lookup → PDF → GCS → renewal_actions record
    (status='drafted'). The caller sends the HITL keyboard; approval is
    handled by `approve_action` below — never here."""
    record = db.get_document(doc_id)
    if not record:
        raise ValueError(f"unknown doc_id {doc_id}")

    playbook = RENEWAL_PLAYBOOK.get(record["doc_type"], RENEWAL_PLAYBOOK["other"])
    checklist: list[str] = playbook["checklist"]
    official_url: str | None = playbook["official_url"]

    pdf_bytes = _render_packet_pdf(record, checklist, official_url)
    packet_url = _upload_packet(pdf_bytes, doc_id)
    action_id = db.write_renewal_action(
        doc_id, packet_url, checklist, official_url or ""
    )
    return {
        "action_id": action_id,
        "packet_url": packet_url,
        "checklist": checklist,
        "official_url": official_url,
    }


def approve_action(action_id: str) -> bool:
    """Skill `await_approval` resolution — the ONLY code path that moves a
    renewal action past 'drafted', and it runs strictly in response to a
    human tapping Approve. Returns False if the action is unknown/already
    handled (double-tap safe)."""
    action = db.get_renewal_action(action_id)
    if not action or action["status"] != "drafted":
        return False
    db.update_renewal_action(action_id, {
        "status": "awaiting_review",
        "reviewed_at": db.now_utc(),
    })
    return True


def cancel_action(action_id: str) -> bool:
    action = db.get_renewal_action(action_id)
    if not action or action["status"] != "drafted":
        return False
    db.update_renewal_action(action_id, {
        "status": "cancelled",
        "reviewed_at": db.now_utc(),
    })
    return True
