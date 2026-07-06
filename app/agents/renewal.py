"""
Agent 4/4 — RENEWAL ACTION AGENT (Design Doc §2.1, §4.4)

Responsibility: when the user asks (or taps /packet), produce a renewal
packet: a step-by-step checklist, the official renewal URL for the issuing
country, and a calendar (.ics) file that mirrors the scheduler's own urgency
boundaries — so the deadline lives in the user's calendar as well as in
LifeKeeper's memory.

⚠ HITL BY CONSTRUCTION (Design Doc §4.4): the agent NEVER automates
third-party portals — no browser sessions, no form submissions on
government/insurer sites. The packet prepares the human to act; the inline
buttons afterwards (renewed / snooze / stop tracking) manage the document's
lifecycle, they never trigger external actions.

MVP note: pre-filled official forms (DS-82, DVLA D1) are post-MVP —
`template_matched` stays honestly False.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .. import firestore_db as db
from .extraction import URGENCY_WINDOWS

log = logging.getLogger("lifekeeper.renewal")

# Checklists per doc type plus official renewal URLs per ISO issuing-country
# code. Only URLs we can stand behind are listed; anything else falls back to
# a checklist that routes the user to the issuer directly.
RENEWAL_PLAYBOOK: dict[str, dict[str, Any]] = {
    "passport": {
        "official_urls": {
            "GB": "https://www.gov.uk/renew-adult-passport",
            "SG": "https://www.ica.gov.sg/documents/passport",
            "US": "https://travel.state.gov/content/travel/en/passports/have-passport/renew.html",
            "AU": "https://www.passports.gov.au/renew",
        },
        "checklist": [
            "Take a new digital passport photo (no glasses, plain background)",
            "Have your current passport handy for its number",
            "Apply online at the official site (about 10 minutes)",
            "Pay the renewal fee online",
            "Follow the instructions for returning or presenting your old passport",
        ],
    },
    "licence": {
        "official_urls": {
            "GB": "https://www.gov.uk/renew-driving-licence",
            "SG": "https://www.police.gov.sg/e-services",
        },
        "checklist": [
            "Have your licence number and national ID handy",
            "Check your address is current and update it during renewal if not",
            "Renew online at the official site",
            "Watch for the new licence in the post within a week or two",
        ],
    },
    "insurance": {
        "official_urls": {},   # issuer-specific; the checklist guides the user
        "checklist": [
            "Find your policy number (it's on the summary above)",
            "Get 2 or 3 comparison quotes before auto-renewing; loyalty rarely pays",
            "Contact your insurer or broker to confirm renewal terms",
            "Make sure the new policy start date leaves no coverage gap",
        ],
    },
    "warranty": {
        "official_urls": {},
        "checklist": [
            "Find your proof of purchase or receipt",
            "Check if the manufacturer offers an extended warranty",
            "Register or extend on the manufacturer's website before expiry",
        ],
    },
    "membership": {
        "official_urls": {},
        "checklist": [
            "Ask yourself if you used this membership enough to renew it",
            "Check for renewal discounts or better tiers",
            "Renew (or cancel!) before the auto-renewal date",
        ],
    },
    "other": {
        "official_urls": {},
        "checklist": [
            "Find the original document and the issuer's contact details",
            "Ask the issuer about their renewal process",
            "Set aside time before the expiry date to complete it",
        ],
    },
}


def get_playbook(doc_type: str, country: Optional[str]) -> tuple[list[str], Optional[str]]:
    """Checklist + official URL for this doc type and issuing country.
    Unknown country (or none on file) → checklist only, no URL."""
    playbook = RENEWAL_PLAYBOOK.get(doc_type, RENEWAL_PLAYBOOK["other"])
    url = playbook["official_urls"].get(country) if country else None
    return playbook["checklist"], url


# ------------------------------------------------------------- calendar ----

def _ics_escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))


def _ics_fold(line: str) -> str:
    """RFC 5545 line folding: max 75 octets per line, continuation lines
    start with a space. Keeps strict parsers (Outlook) happy."""
    out = []
    while len(line) > 74:
        out.append(line[:74])
        line = " " + line[74:]
    out.append(line)
    return "\r\n".join(out)


def _ics_event(uid: str, date: datetime, summary: str, description: str) -> list[str]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return [
        "BEGIN:VEVENT",
        f"UID:{uid}@lifekeeper",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{date:%Y%m%d}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{_ics_escape(summary)}",
        "TRIGGER:-PT15H",   # ~9am the day before an all-day event
        "END:VALARM",
        "END:VEVENT",
    ]


def build_calendar_ics(record: dict[str, Any]) -> str:
    """The packet's calendar half: an .ics mirroring the scheduler's tier
    boundaries. One event on the 'start renewing now' date (expiry minus the
    early-warning window, skipped if already past) and one on the expiry
    itself — the calendar becomes an offline copy of the agent's own
    reminder logic."""
    doc_type: str = record["doc_type"]
    expiry: datetime = record["expiry_date"]
    windows = URGENCY_WINDOWS.get(doc_type, URGENCY_WINDOWS["other"])
    checklist, official_url = get_playbook(doc_type, record.get("issuing_country"))

    description = "Checklist:\n" + "\n".join(
        f"{i}. {step}" for i, step in enumerate(checklist, 1)
    )
    if official_url:
        description += f"\nOfficial renewal site: {official_url}"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//LifeKeeper//Renewal Packet//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    start = expiry - timedelta(days=windows["early_warning"])
    if start > datetime.now(timezone.utc):
        lines += _ics_event(
            f"{record['doc_id']}-start",
            start,
            f"Start {doc_type} renewal (expires {expiry:%d %b %Y})",
            description,
        )
    lines += _ics_event(
        f"{record['doc_id']}-expiry",
        expiry,
        f"{doc_type.title()} EXPIRES today",
        description,
    )
    lines.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(line) for line in lines) + "\r\n"


# --------------------------------------------------------------- packet ----

def generate_renewal_packet(doc_id: str) -> dict[str, Any]:
    """Full packet flow: playbook lookup → calendar file → renewal_actions
    audit record. Returns everything the caller needs to present the packet;
    what happens next is the user's call (renewed / snooze / stop buttons)."""
    record = db.get_document(doc_id)
    if not record:
        raise ValueError(f"unknown doc_id {doc_id}")

    checklist, official_url = get_playbook(
        record["doc_type"], record.get("issuing_country")
    )
    calendar_ics = build_calendar_ics(record)
    action_id = db.write_renewal_action(doc_id, "", checklist, official_url or "")
    return {
        "action_id": action_id,
        "doc_id": doc_id,
        "doc_type": record["doc_type"],
        "expiry_date": record["expiry_date"],
        "issuing_country": record.get("issuing_country"),
        "checklist": checklist,
        "official_url": official_url,
        "calendar_ics": calendar_ics,
    }


def approve_action(action_id: str) -> bool:
    """Kept for the MCP `/mcp/tools/approve_action` contract (Design Doc §5)
    and the ADK agents. Returns False if the action is unknown or already
    handled (double-tap safe via a Firestore transaction)."""
    return db.transition_renewal_action(action_id, {
        "status": "awaiting_review",
        "reviewed_at": db.now_utc(),
    })


def cancel_action(action_id: str) -> bool:
    return db.transition_renewal_action(action_id, {
        "status": "cancelled",
        "reviewed_at": db.now_utc(),
    })
