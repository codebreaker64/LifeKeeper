"""End-to-end scheduler test against the Firestore emulator — verifies the
core tier-crossing invariant with a REAL Firestore query path (composite
index semantics included), only Telegram is faked.

Run:
    gcloud emulators firestore start --host-port=localhost:8081
    # separate shell:
    $env:FIRESTORE_EMULATOR_HOST = "localhost:8081"        (PowerShell)
    python -m pytest tests/test_integration_emulator.py -q

Skipped automatically when the emulator env var is absent.
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("FIRESTORE_EMULATOR_HOST"),
    reason="Firestore emulator not running (set FIRESTORE_EMULATOR_HOST)",
)

from app import firestore_db as db            # noqa: E402
from app.agents import scheduler              # noqa: E402


def test_tier_crossing_notifies_exactly_once(monkeypatch):
    sent: list[str] = []

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append(text)

    monkeypatch.setattr(scheduler.tg, "send_message", fake_send)

    # Fresh user + a passport 89 days out (inside the 90-day early_warning
    # window) that is due for a check right now.
    user = db.get_or_create_user(f"emu-{uuid.uuid4().hex[:8]}")
    now = datetime.now(timezone.utc)
    doc_id = db.write_document_record({
        "user_id": user["user_id"],
        "doc_type": "passport",
        "expiry_date": now + timedelta(days=89),
        "confidence": 0.95,
        "next_check_at": now - timedelta(minutes=1),
    })

    # Pass 1: our document crosses null → early_warning and notifies.
    asyncio.run(scheduler.run_scheduler_pass())
    ours = [m for m in sent if "Passport" in m]
    assert len(ours) == 1
    assert "Early warning" in ours[0]

    saved = db.get_document(doc_id)
    assert saved["last_notified_tier"] == "early_warning"
    # next_check_at advanced to the final_reminder boundary (expiry - 30d).
    assert saved["next_check_at"] > now

    # Pass 2: nothing new — the anti-spam invariant.
    sent.clear()
    asyncio.run(scheduler.run_scheduler_pass())
    assert sent == []
