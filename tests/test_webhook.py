"""FastAPI-layer tests: webhook auth, dedupe, HITL callbacks, scheduler and
MCP endpoint auth. Firestore, Telegram, and the agents are faked — this
exercises the routing/guard logic in app/main.py only."""

import hashlib
import itertools

from fastapi.testclient import TestClient

from app import main
from app.config import get_settings

client = TestClient(main.app)

_update_ids = itertools.count(1000)  # unique per request — SEEN_UPDATES is global


def _webhook_path() -> str:
    token = get_settings().telegram_bot_token
    return "/webhook/" + hashlib.sha256(token.encode()).hexdigest()[:32]


def _webhook_headers() -> dict[str, str]:
    secret = get_settings().telegram_webhook_secret
    return {"X-Telegram-Bot-Api-Secret-Token": secret} if secret else {}


def _post_update(payload: dict) -> object:
    payload.setdefault("update_id", next(_update_ids))
    return client.post(_webhook_path(), json=payload, headers=_webhook_headers())


class _SentLog:
    """Async fakes for the telegram client, recording every call."""

    def __init__(self, monkeypatch):
        self.messages: list[str] = []
        self.callbacks: list[str] = []

        async def fake_send(chat_id, text, reply_markup=None):
            self.messages.append(text)

        async def fake_answer(callback_id, text=""):
            self.callbacks.append(text)

        monkeypatch.setattr(main.tg, "send_message", fake_send)
        monkeypatch.setattr(main.tg, "answer_callback", fake_answer)
        monkeypatch.setattr(
            main.db, "get_or_create_user", lambda chat_id: {"user_id": "u-test"}
        )


# ------------------------------------------------------------ webhook auth

def test_wrong_token_hash_is_403():
    r = client.post("/webhook/deadbeef" + "0" * 24, json={"update_id": 1})
    assert r.status_code == 403


def test_start_command_replies(monkeypatch):
    log = _SentLog(monkeypatch)
    r = _post_update({"message": {"chat": {"id": 42}, "text": "/start"}})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert len(log.messages) == 1
    assert "LifeKeeper" in log.messages[0]


def test_duplicate_update_id_is_dropped(monkeypatch):
    log = _SentLog(monkeypatch)
    update = {
        "update_id": next(_update_ids),
        "message": {"chat": {"id": 42}, "text": "/start"},
    }
    client.post(_webhook_path(), json=update, headers=_webhook_headers())
    client.post(_webhook_path(), json=update, headers=_webhook_headers())
    assert len(log.messages) == 1  # second delivery deduped before handling


def test_handler_crash_never_500s(monkeypatch):
    _SentLog(monkeypatch)

    async def boom(user_id, text):
        raise RuntimeError("boom")

    monkeypatch.setattr(main.scheduler, "handle_query", boom)
    r = _post_update({"message": {"chat": {"id": 42}, "text": "what expires soon?"}})
    assert r.status_code == 200  # crash logged + apology sent, never a 500


# ------------------------------------------------------------ HITL callbacks

def test_approve_callback(monkeypatch):
    log = _SentLog(monkeypatch)
    monkeypatch.setattr(main.renewal, "approve_action", lambda action_id: True)
    r = _post_update({"callback_query": {
        "id": "cb1", "data": "approve:abc123",
        "message": {"chat": {"id": 42}},
    }})
    assert r.status_code == 200
    assert log.callbacks == ["Approved ✅"]
    assert any("Approved" in m for m in log.messages)


def test_double_tap_approve_reports_already_handled(monkeypatch):
    log = _SentLog(monkeypatch)
    monkeypatch.setattr(main.renewal, "approve_action", lambda action_id: False)
    r = _post_update({"callback_query": {
        "id": "cb2", "data": "approve:abc123",
        "message": {"chat": {"id": 42}},
    }})
    assert r.status_code == 200
    assert log.callbacks == ["Already handled"]
    assert log.messages == []  # no duplicate confirmation message


def test_malformed_callback_never_500s(monkeypatch):
    _SentLog(monkeypatch)
    # Telegram can omit `message` on very old callback queries.
    r = _post_update({"callback_query": {"id": "cb3", "data": "approve:xyz"}})
    assert r.status_code == 200


def test_expired_confirmation(monkeypatch):
    log = _SentLog(monkeypatch)
    r = _post_update({"callback_query": {
        "id": "cb4", "data": "confirmdoc:not-in-memory",
        "message": {"chat": {"id": 42}},
    }})
    assert r.status_code == 200
    assert any("expired" in m for m in log.messages)


# ------------------------------------------------- lifecycle + packet flow

def test_renewed_callback_closes_document(monkeypatch):
    log = _SentLog(monkeypatch)
    updated: dict = {}
    monkeypatch.setattr(
        main.db, "update_document",
        lambda doc_id, fields: updated.setdefault(doc_id, fields),
    )
    r = _post_update({"callback_query": {
        "id": "cb10", "data": "renewed:doc42",
        "message": {"chat": {"id": 42}},
    }})
    assert r.status_code == 200
    assert updated["doc42"] == {"status": "renewed"}
    assert any("new document" in m for m in log.messages)


def test_snooze_callback(monkeypatch):
    log = _SentLog(monkeypatch)
    snoozed: list = []
    monkeypatch.setattr(
        main.scheduler, "snooze_document",
        lambda doc_id, days=7: snoozed.append(doc_id) or True,
    )
    r = _post_update({"callback_query": {
        "id": "cb11", "data": "snooze:doc42",
        "message": {"chat": {"id": 42}},
    }})
    assert r.status_code == 200
    assert snoozed == ["doc42"]
    assert any("week" in m for m in log.messages)


def test_stop_callback_archives_document(monkeypatch):
    log = _SentLog(monkeypatch)
    updated: dict = {}
    monkeypatch.setattr(
        main.db, "update_document",
        lambda doc_id, fields: updated.setdefault(doc_id, fields),
    )
    r = _post_update({"callback_query": {
        "id": "cb12", "data": "stop:doc42",
        "message": {"chat": {"id": 42}},
    }})
    assert r.status_code == 200
    assert updated["doc42"] == {"status": "archived"}
    assert any("stopped tracking" in m for m in log.messages)


def test_packet_command_with_no_documents(monkeypatch):
    log = _SentLog(monkeypatch)
    monkeypatch.setattr(main.db, "list_user_documents", lambda user_id: [])
    r = _post_update({"message": {"chat": {"id": 42}, "text": "/packet"}})
    assert r.status_code == 200
    assert any("Send me a document" in m for m in log.messages)


def test_country_answer_updates_doc_and_user_default(monkeypatch):
    log = _SentLog(monkeypatch)
    doc_updates: dict = {}
    user_updates: dict = {}
    monkeypatch.setattr(
        main.db, "update_document",
        lambda doc_id, fields: doc_updates.setdefault(doc_id, fields),
    )
    monkeypatch.setattr(
        main.db, "update_user",
        lambda user_id, fields: user_updates.setdefault(user_id, fields),
    )
    main.PENDING_COUNTRY["42"] = ("doc7", "u-test")
    r = _post_update({"message": {"chat": {"id": 42}, "text": "Singapore"}})
    assert r.status_code == 200
    assert doc_updates["doc7"] == {"issuing_country": "SG"}
    assert user_updates["u-test"] == {"default_country": "SG"}
    assert "42" not in main.PENDING_COUNTRY
    assert any("SG" in m for m in log.messages)


def test_saved_message_warns_on_expired_document():
    from datetime import datetime, timedelta, timezone

    expired = {"expiry_date": datetime.now(timezone.utc) - timedelta(days=30)}
    msg = main._saved_message(expired, "summary")
    assert "already expired" in msg and "/packet" in msg

    future = {"expiry_date": datetime.now(timezone.utc) + timedelta(days=300)}
    msg = main._saved_message(future, "summary")
    assert "Saved!" in msg and "expired" not in msg


def test_help_command(monkeypatch):
    log = _SentLog(monkeypatch)
    r = _post_update({"message": {"chat": {"id": 42}, "text": "/help"}})
    assert r.status_code == 200
    assert any("/packet" in m for m in log.messages)


# ------------------------------------------------------- scheduler endpoint

def test_scheduler_requires_token():
    assert client.post("/tasks/run-scheduler").status_code == 403
    assert client.post(
        "/tasks/run-scheduler", headers={"X-Scheduler-Token": "wrong"}
    ).status_code == 403


def test_scheduler_runs_with_token(monkeypatch):
    async def fake_pass():
        return {"checked": 3, "notified": 1}

    monkeypatch.setattr(main.scheduler, "run_scheduler_pass", fake_pass)
    r = client.post(
        "/tasks/run-scheduler",
        headers={"X-Scheduler-Token": get_settings().scheduler_token},
    )
    assert r.status_code == 200
    assert r.json() == {"checked": 3, "notified": 1}


# ------------------------------------------------------------ MCP endpoints

def test_mcp_requires_api_token():
    r = client.post("/mcp/tools/get_records", json={"user_id": "u1"})
    assert r.status_code == 403


def test_mcp_get_records(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(main.db, "list_user_documents", lambda user_id: [{
        "doc_id": "d1", "doc_type": "passport",
        "expiry_date": datetime(2030, 5, 1, tzinfo=timezone.utc),
        "next_check_at": datetime(2030, 2, 1, tzinfo=timezone.utc),
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }])
    r = client.post(
        "/mcp/tools/get_records",
        json={"user_id": "u1"},
        headers={"X-API-Token": get_settings().scheduler_token},
    )
    assert r.status_code == 200
    records = r.json()["records"]
    assert records[0]["expiry_date"].startswith("2030-05-01")
    assert "next_check_at" not in records[0]  # internal fields stripped
