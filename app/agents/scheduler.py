"""
Agent 3/4 — REMINDER & SCHEDULER AGENT (Design Doc §2.1, §4.3)

Responsibility: LONG-HORIZON reasoning — this agent thinks in weeks and
months, not in the next chat turn. Fired by Cloud Scheduler cron (daily
07:00 UTC) via POST /tasks/run-scheduler, or manually for demos.

Core mechanic (Design Doc §3.2):
    tier crossing — a document moves null → early_warning → final_reminder
    → overdue as expiry approaches. We notify ONLY when the tier crosses,
    comparing against `last_notified_tier`, so a daily cron never spams.

Skills implemented:
    query_due_records / compute_tier / check_tier_crossing /
    update_next_check_at / notify / handle_query
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .. import firestore_db as db
from .. import telegram_client as tg
from .extraction import URGENCY_WINDOWS

log = logging.getLogger("lifekeeper.scheduler")

TIER_ORDER = [None, "early_warning", "final_reminder", "overdue"]

TIER_MESSAGES = {
    "early_warning": "⏰ *Early warning* — your {doc_type} ({ref}) expires on "
                     "{expiry}. That's in {days} days. Reply "
                     "*generate my renewal packet* when you're ready.",
    "final_reminder": "🚨 *Final reminder* — your {doc_type} ({ref}) expires on "
                      "{expiry} — only {days} days left! Reply "
                      "*generate my renewal packet* to get started now.",
    "overdue": "❗ *Overdue* — your {doc_type} ({ref}) expired on {expiry}. "
               "Reply *generate my renewal packet* and I'll prepare "
               "everything you need to fix this.",
}


def compute_tier(expiry_date: datetime, doc_type: str) -> Optional[str]:
    """Skill `compute_tier`: which urgency tier is this record in *right now*?"""
    windows = URGENCY_WINDOWS.get(doc_type, URGENCY_WINDOWS["other"])
    now = datetime.now(timezone.utc)
    if now >= expiry_date:
        return "overdue"
    if now >= expiry_date - timedelta(days=windows["final_reminder"]):
        return "final_reminder"
    if now >= expiry_date - timedelta(days=windows["early_warning"]):
        return "early_warning"
    return None


def check_tier_crossing(record: dict[str, Any], new_tier: Optional[str]) -> bool:
    """Skill `check_tier_crossing`: notify only when urgency has *increased*
    since the last notification — the anti-spam invariant."""
    last = record.get("last_notified_tier")
    return TIER_ORDER.index(new_tier) > TIER_ORDER.index(last)


def _advance_next_check_at(record: dict[str, Any], current_tier: Optional[str]) -> datetime:
    """Skill `update_next_check_at`: after handling a record, schedule the
    next look at the *next* tier boundary ahead."""
    windows = URGENCY_WINDOWS.get(record["doc_type"], URGENCY_WINDOWS["other"])
    expiry: datetime = record["expiry_date"]
    boundaries = {
        None: expiry - timedelta(days=windows["early_warning"]),
        "early_warning": expiry - timedelta(days=windows["final_reminder"]),
        "final_reminder": expiry,
        "overdue": expiry + timedelta(days=365 * 10),  # terminal — park it
    }
    return boundaries[current_tier]


async def notify(record: dict[str, Any], tier: str) -> None:
    """Skill `notify` — channel-agnostic by design (§4.3). MVP ships
    Telegram; SendGrid/Twilio can be added here without touching the loop."""
    user = db.get_user(record["user_id"])
    if not user or not user.get("telegram_chat_id"):
        log.warning("No telegram_chat_id for user %s — queueing skipped", record["user_id"])
        return
    days = max((record["expiry_date"] - datetime.now(timezone.utc)).days, 0)
    text = TIER_MESSAGES[tier].format(
        doc_type=record["doc_type"].title(),
        ref=record.get("reference_number") or "no ref",
        expiry=record["expiry_date"].strftime("%d %b %Y"),
        days=days,
    )
    await tg.send_message(user["telegram_chat_id"], text)


async def run_scheduler_pass() -> dict[str, int]:
    """One full scheduler pass. Idempotent: run it twice in a row and the
    second pass notifies nobody (tier-crossing invariant). Returns counters
    for logging/demo output."""
    checked = notified = 0
    for record in db.query_due_records():
        checked += 1
        tier = compute_tier(record["expiry_date"], record["doc_type"])
        if tier and check_tier_crossing(record, tier):
            await notify(record, tier)
            db.update_document(record["doc_id"], {"last_notified_tier": tier})
            notified += 1
        db.update_document(
            record["doc_id"],
            {"next_check_at": _advance_next_check_at(record, tier)},
        )
    log.info("Scheduler pass: checked=%d notified=%d", checked, notified)
    return {"checked": checked, "notified": notified}


async def handle_query(user_id: str, message: str) -> str:
    """Skill `handle_query` (§4.3, P2 demo value): answer conversational
    questions like 'what expires in 3 months?' from Firestore memory.
    A deliberate non-LLM implementation: list everything, soonest first —
    correct, instant, and zero hallucination risk for the demo."""
    docs = db.list_user_documents(user_id)
    if not docs:
        return ("You haven't saved any documents yet. Send me a photo or PDF "
                "of a passport, licence, insurance policy, warranty, or "
                "membership card to get started!")
    docs.sort(key=lambda d: d["expiry_date"])
    lines = ["📋 *Your tracked documents:*"]
    for d in docs:
        days = (d["expiry_date"] - datetime.now(timezone.utc)).days
        status = f"expires in {days} days" if days >= 0 else f"expired {-days} days ago ❗"
        lines.append(
            f"• *{d['doc_type'].title()}* ({d.get('reference_number') or 'no ref'}) — "
            f"{d['expiry_date'].strftime('%d %b %Y')} ({status})"
        )
    lines.append("\nReply *generate my renewal packet* to act on the most urgent one.")
    return "\n".join(lines)
