"""
seed_demo.py — plant demo documents whose expiry dates sit *just inside*
tier boundaries, so the very next scheduler pass fires a notification
on camera. Judges can't wait 90 real days (Demo Script §8, step 3).

Usage (after at least one /start so your user exists):
    python scripts/seed_demo.py <telegram_chat_id>

Then trigger the scheduler:
    curl -X POST $SERVICE_URL/tasks/run-scheduler -H "X-Scheduler-Token: $SCHEDULER_TOKEN"
"""

import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")  # run from repo root

from app import firestore_db as db  # noqa: E402


def main(chat_id: str) -> None:
    user = db.get_or_create_user(chat_id)
    now = datetime.now(timezone.utc)

    demos = [
        # Passport 89 days out → inside the 90-day early_warning window.
        dict(doc_type="passport", expiry_date=now + timedelta(days=89),
             owner_name="Demo User", reference_number="P-99001122",
             issuer="HMPO", confidence=0.94),
        # Insurance 13 days out → inside the 14-day final_reminder window.
        dict(doc_type="insurance", expiry_date=now + timedelta(days=13),
             owner_name="Demo User", reference_number="POL-778899",
             issuer="Aviva", confidence=0.91),
    ]
    for d in demos:
        d.update(
            user_id=user["user_id"],
            next_check_at=now - timedelta(minutes=1),  # due immediately
            last_notified_tier=None,
        )
        doc_id = db.write_document_record(d)
        print(f"Seeded {d['doc_type']} ({doc_id}) expiring {d['expiry_date']:%d %b %Y}")

    print("\nNow fire the scheduler — both documents should notify exactly once.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/seed_demo.py <telegram_chat_id>")
    main(sys.argv[1])
