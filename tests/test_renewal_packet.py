"""Renewal Action Agent: country-aware playbook lookups and the .ics
calendar half of the packet. Pure logic — no GCP, no Telegram."""

from datetime import datetime, timedelta, timezone

from app.agents.extraction import country_is_relevant
from app.agents.renewal import build_calendar_ics, get_playbook


def test_country_only_relevant_for_passport_and_licence():
    assert country_is_relevant("passport")
    assert country_is_relevant("licence")
    for t in ("insurance", "warranty", "membership", "other"):
        assert not country_is_relevant(t)


def _record(days_to_expiry: int, doc_type: str = "passport", country: str = "GB"):
    return {
        "doc_id": "doc1",
        "doc_type": doc_type,
        "expiry_date": datetime.now(timezone.utc) + timedelta(days=days_to_expiry),
        "reference_number": "P-123",
        "owner_name": "Ada Lovelace",
        "issuing_country": country,
    }


# -------------------------------------------------------------- playbook ---

def test_playbook_matches_issuing_country():
    _, url = get_playbook("passport", "SG")
    assert url and "ica.gov.sg" in url
    _, url = get_playbook("passport", "GB")
    assert url and "gov.uk" in url


def test_playbook_unknown_country_gives_checklist_only():
    checklist, url = get_playbook("passport", "ZZ")
    assert url is None
    assert checklist  # generic steps still provided


def test_playbook_no_country_gives_checklist_only():
    checklist, url = get_playbook("insurance", None)
    assert url is None
    assert checklist


def test_playbook_unknown_doc_type_falls_back_to_other():
    checklist, _ = get_playbook("spaceship", "GB")
    assert checklist == get_playbook("other", "GB")[0]


# -------------------------------------------------------------- calendar ---

def test_ics_has_start_and_expiry_events_when_far_out():
    # 200 days out: the 90-day "start renewing" boundary is still ahead.
    ics = build_calendar_ics(_record(200))
    assert ics.count("BEGIN:VEVENT") == 2
    assert "BEGIN:VCALENDAR" in ics and "END:VCALENDAR" in ics
    assert "Start passport renewal" in ics
    assert "EXPIRES today" in ics


def test_ics_drops_start_event_when_boundary_passed():
    # 13 days out: the early-warning boundary is behind us — expiry only.
    ics = build_calendar_ics(_record(13))
    assert ics.count("BEGIN:VEVENT") == 1
    assert "EXPIRES today" in ics


def test_ics_events_are_all_day_on_the_right_dates():
    record = _record(200)
    ics = build_calendar_ics(record)
    expiry = record["expiry_date"]
    start = expiry - timedelta(days=90)  # passport early_warning window
    assert f"DTSTART;VALUE=DATE:{expiry:%Y%m%d}" in ics
    assert f"DTSTART;VALUE=DATE:{start:%Y%m%d}" in ics


def test_ics_official_url_lands_in_description():
    ics = build_calendar_ics(_record(200, country="GB"))
    assert "gov.uk" in ics


def test_ics_lines_are_folded_to_spec():
    # RFC 5545: no line longer than 75 octets (folded lines excluded).
    ics = build_calendar_ics(_record(200))
    assert all(len(line) <= 75 for line in ics.split("\r\n"))
