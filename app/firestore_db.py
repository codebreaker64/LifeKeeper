"""
firestore_db.py — LifeKeeper's long-term memory (rubric: "Long-term memory").

Implements the three collections from Design Doc §3:
    users/{user_id}              — telegram chat mapping + notify channels
    documents/{doc_id}           — extracted records with urgency scheduling
    renewal_actions/{action_id}  — HITL-gated renewal packets

Key design decisions carried over from the design doc §3.2:
  * `last_notified_tier` — scheduler only notifies on *tier crossings*,
    never re-fires the same alert on every cron run.
  * `next_check_at` — scheduler queries WHERE next_check_at <= now()
    instead of scanning all documents (requires the composite index
    created in deploy.sh).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from google.cloud import firestore

_db: Optional[firestore.Client] = None


def db() -> firestore.Client:
    """Lazy singleton — Cloud Run cold-starts faster when the client is
    created on first use rather than at import time."""
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- users ----

def get_or_create_user(telegram_chat_id: str) -> dict[str, Any]:
    """Map a Telegram chat to a stable user_id. Created on first /start —
    Telegram bots cannot initiate conversations (Design Doc §5.2 note)."""
    q = (
        db().collection("users")
        .where("telegram_chat_id", "==", str(telegram_chat_id))
        .limit(1)
        .stream()
    )
    for snap in q:
        return {"user_id": snap.id, **snap.to_dict()}

    user_id = uuid.uuid4().hex[:12]
    record = {
        "telegram_chat_id": str(telegram_chat_id),
        "notify_channels": ["telegram"],   # email/SMS are post-MVP stretch
        "contact_email": None,
        "contact_phone": None,
        # Default issuing country for playbook lookups — learned from the
        # first document (or the user's answer), asked at most once.
        "default_country": None,
        "onboarded_at": now_utc(),
    }
    db().collection("users").document(user_id).set(record)
    return {"user_id": user_id, **record}


def get_user(user_id: str) -> Optional[dict[str, Any]]:
    snap = db().collection("users").document(user_id).get()
    return {"user_id": snap.id, **snap.to_dict()} if snap.exists else None


def update_user(user_id: str, fields: dict[str, Any]) -> None:
    db().collection("users").document(user_id).update(fields)


# ----------------------------------------------------------- documents ----

def write_document_record(record: dict[str, Any]) -> str:
    """Extraction Agent skill `write_document_record` (Design Doc §4.2).
    Validates required fields before persisting — never write junk."""
    required = {"user_id", "doc_type", "expiry_date", "confidence"}
    missing = required - record.keys()
    if missing:
        raise ValueError(f"document record missing fields: {missing}")

    doc_id = uuid.uuid4().hex[:12]
    record.setdefault("owner_name", None)
    record.setdefault("reference_number", None)
    record.setdefault("issuer", None)
    record.setdefault("issuing_country", None)
    record.setdefault("template_matched", False)
    record.setdefault("source_file_ref", None)
    record.setdefault("last_notified_tier", None)
    record.setdefault("status", "active")
    record.setdefault("created_at", now_utc())
    db().collection("documents").document(doc_id).set(record)
    return doc_id


def get_document(doc_id: str) -> Optional[dict[str, Any]]:
    snap = db().collection("documents").document(doc_id).get()
    return {"doc_id": snap.id, **snap.to_dict()} if snap.exists else None


def update_document(doc_id: str, fields: dict[str, Any]) -> None:
    db().collection("documents").document(doc_id).update(fields)


def query_due_records() -> list[dict[str, Any]]:
    """Scheduler Agent skill `query_due_records` (Design Doc §4.3).
    Composite index on (status, next_check_at) required — see deploy.sh."""
    q = (
        db().collection("documents")
        .where("status", "==", "active")
        .where("next_check_at", "<=", now_utc())
        .stream()
    )
    return [{"doc_id": s.id, **s.to_dict()} for s in q]


def list_user_documents(user_id: str, status: str = "active") -> list[dict[str, Any]]:
    q = (
        db().collection("documents")
        .where("user_id", "==", user_id)
        .where("status", "==", status)
        .stream()
    )
    return [{"doc_id": s.id, **s.to_dict()} for s in q]


# ------------------------------------------------------ renewal_actions ----

def write_renewal_action(
    doc_id: str, packet_url: str, checklist: list[str], official_url: str
) -> str:
    """Renewal Agent skill `write_renewal_action` (Design Doc §4.4).
    Actions are born 'drafted' — the HITL gate moves them forward."""
    action_id = uuid.uuid4().hex[:12]
    db().collection("renewal_actions").document(action_id).set({
        "doc_id": doc_id,
        "packet_url": packet_url,
        "checklist": checklist,
        "official_url": official_url,
        "status": "drafted",
        "created_at": now_utc(),
        "reviewed_at": None,
    })
    return action_id


def get_renewal_action(action_id: str) -> Optional[dict[str, Any]]:
    snap = db().collection("renewal_actions").document(action_id).get()
    return {"action_id": snap.id, **snap.to_dict()} if snap.exists else None


def update_renewal_action(action_id: str, fields: dict[str, Any]) -> None:
    db().collection("renewal_actions").document(action_id).update(fields)


def transition_renewal_action(action_id: str, fields: dict[str, Any]) -> bool:
    """Atomically move an action out of 'drafted' — the HITL gate resolution.
    A Firestore transaction makes the read-check-write race-free, so two
    simultaneous Approve taps can never both succeed (double-tap safe)."""
    ref = db().collection("renewal_actions").document(action_id)
    transaction = db().transaction()

    @firestore.transactional
    def _txn(txn: firestore.Transaction) -> bool:
        snap = ref.get(transaction=txn)
        if not snap.exists or snap.to_dict().get("status") != "drafted":
            return False
        txn.update(ref, fields)
        return True

    return _txn(transaction)
