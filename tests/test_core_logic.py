"""
test_core_logic.py — offline tests for the pure business logic.
No GCP credentials needed; run with:  python -m pytest tests/ -q

These cover the two invariants the whole system depends on:
  1. Tier computation matches the urgency-window table (§4.2).
  2. Tier-crossing check never re-notifies (anti-spam, §3.2).
"""

from datetime import datetime, timedelta, timezone

from app.agents.extraction import compute_next_check_at
from app.agents.scheduler import check_tier_crossing, compute_tier


def _days(n: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=n)


# ------------------------------------------------------------ compute_tier

def test_passport_tiers():
    assert compute_tier(_days(120), "passport") is None
    assert compute_tier(_days(89), "passport") == "early_warning"
    assert compute_tier(_days(29), "passport") == "final_reminder"
    assert compute_tier(_days(-1), "passport") == "overdue"


def test_warranty_tiers():
    assert compute_tier(_days(31), "warranty") is None
    assert compute_tier(_days(29), "warranty") == "early_warning"
    assert compute_tier(_days(6), "warranty") == "final_reminder"


def test_unknown_doc_type_uses_other_windows():
    assert compute_tier(_days(29), "other") == "early_warning"


# ----------------------------------------------------- check_tier_crossing

def test_notifies_on_first_crossing():
    record = {"last_notified_tier": None}
    assert check_tier_crossing(record, "early_warning") is True


def test_never_renotifies_same_tier():
    record = {"last_notified_tier": "early_warning"}
    assert check_tier_crossing(record, "early_warning") is False


def test_notifies_on_escalation_only():
    record = {"last_notified_tier": "early_warning"}
    assert check_tier_crossing(record, "final_reminder") is True
    record = {"last_notified_tier": "overdue"}
    assert check_tier_crossing(record, "final_reminder") is False


# --------------------------------------------------- compute_next_check_at

def test_next_check_is_next_boundary_ahead():
    expiry = _days(120)
    nxt = compute_next_check_at(expiry, "passport")
    # 120 days out → next boundary is the 90-day early_warning mark.
    assert abs((nxt - (expiry - timedelta(days=90))).total_seconds()) < 5


def test_expired_document_checks_immediately():
    nxt = compute_next_check_at(_days(-10), "passport")
    assert nxt <= datetime.now(timezone.utc) + timedelta(seconds=5)
