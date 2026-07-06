"""
main.py — LifeKeeper's single Cloud Run service (MVP consolidation).

The design doc's four services (pipeline / renewal / scheduler / telegram)
are consolidated into ONE deployable FastAPI app for the MVP. The four
AGENTS remain distinct modules under app/agents/ — the multi-agent
architecture lives in the code, the single service just hosts it.

Routes:
    POST /webhook/{token_hash}     Telegram Bot API webhook (messages + HITL taps)
    POST /tasks/run-scheduler      Cloud Scheduler cron target (+ manual demo)
    POST /mcp/tools/{tool}         MCP-style tool endpoints (Design Doc §5)
    GET  /healthz                  Cloud Run health check

SECURITY:
    * Webhook path embeds a hash of the bot token — unguessable URL, so
      random POSTs can't impersonate Telegram (Design Doc §6.3).
    * Scheduler endpoint requires the X-Scheduler-Token shared secret.
    * All file guardrails enforced inside the Ingestion Agent.
"""

from __future__ import annotations


import asyncio
import hashlib
import hmac
import logging
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from . import firestore_db as db
from . import telegram_client as tg
from .agents import extraction, renewal, scheduler
from .agents.extraction import ExtractionError
from .agents.ingestion import IngestionError, ingest
from .config import get_settings

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lifekeeper.main")

app = FastAPI(title="LifeKeeper", version="1.0.0")

# In-memory holding area for low-confidence extractions awaiting the user's
# confirm tap. Acceptable for MVP (deploy.sh pins --max-instances 1 so taps
# always land on the instance holding the record); move to a Firestore
# `pending_extractions` collection for multi-instance. Capped LRU so users
# who never tap confirm/discard can't grow it forever.
PENDING_EXTRACTIONS: OrderedDict[str, dict[str, Any]] = OrderedDict()
MAX_PENDING = 500

# Chats we've asked "which country issued this?" — maps chat_id to the
# (doc_id, user_id) awaiting the answer. Same MVP caveats as above.
PENDING_COUNTRY: OrderedDict[str, tuple[str, str]] = OrderedDict()

# LRU of processed Telegram update_ids — Telegram redelivers on error/timeout;
# never process the same update twice. NOTE: ids are recorded BEFORE handling,
# so a crashed handler is NOT retried on redelivery (at-most-once by design —
# a poison update must not cause a redelivery storm).
SEEN_UPDATES: OrderedDict[int, None] = OrderedDict()

# Fire-and-forget pipeline tasks need a strong reference until done, or the
# event loop may garbage-collect them mid-flight (documented asyncio pitfall).
_BG_TASKS: set[asyncio.Task] = set()


def _token_hash() -> str:
    return hashlib.sha256(
        get_settings().telegram_bot_token.encode()
    ).hexdigest()[:32]


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# ================================================== Telegram webhook =======

@app.post("/webhook/{token_hash}")
async def telegram_webhook(token_hash: str, request: Request) -> dict[str, bool]:
    if not hmac.compare_digest(token_hash, _token_hash()):
        raise HTTPException(status_code=403)

    # Telegram's official webhook auth (registered via setWebhook secret_token
    # in deploy.sh) — enforced only when configured, on top of the hash path.
    secret = get_settings().telegram_webhook_secret
    if secret and not hmac.compare_digest(
        request.headers.get("x-telegram-bot-api-secret-token", ""), secret
    ):
        raise HTTPException(status_code=403)

    update = await request.json()

    # ---- Dedupe: drop redelivered updates before doing any work ----
    update_id = update.get("update_id")
    if update_id is not None:
        if update_id in SEEN_UPDATES:
            return {"ok": True}
        SEEN_UPDATES[update_id] = None
        if len(SEEN_UPDATES) > 1000:
            SEEN_UPDATES.popitem(last=False)   # evict oldest

    if "callback_query" in update:
        # A malformed callback (e.g. no `message` on very old updates) must
        # never 500 the webhook.
        try:
            await _handle_callback(update["callback_query"])
        except Exception:
            log.exception("Callback handler failed")
        return {"ok": True}

    message = update.get("message") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    if not chat_id:
        return {"ok": True}

    user = await asyncio.to_thread(db.get_or_create_user, chat_id)

    file_id = _extract_file_id(message)
    if file_id:
        # ACK Telegram immediately; run the heavy Document AI / Gemini
        # pipeline in the background so a slow or crashing pipeline can
        # never cause a webhook timeout → redelivery storm.
        task = asyncio.create_task(_handle_document(user, chat_id, file_id))
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
        return {"ok": True}

    text = (message.get("text") or "").strip()
    try:
        await _handle_text(user, chat_id, text)
    except Exception:
        # A handler crash must never 500 the webhook (Telegram would
        # redeliver) — log it and tell the user instead of going silent.
        log.exception("Text handler failed for chat %s", chat_id)
        await tg.send_message(chat_id, "Sorry, something went wrong. Please try again.")
    return {"ok": True}


def _extract_file_id(message: dict[str, Any]) -> str | None:
    """Telegram sends photos as an array of sizes — take the largest.
    PDFs arrive as `document` attachments."""
    if "photo" in message:
        return message["photo"][-1]["file_id"]
    if "document" in message:
        return message["document"]["file_id"]
    return None


async def _handle_document(user: dict, chat_id: str, file_id: str) -> None:
    """Ingestion → Extraction pipeline for one uploaded document."""
    await tg.send_message(chat_id, "📄 Got it — reading your document…")
    try:
        data, mime = await tg.fetch_file(file_id)
        # Document AI / Gemini calls are synchronous — run them in a worker
        # thread so the event loop keeps serving other webhooks meanwhile.
        ocr = await asyncio.to_thread(ingest, data, mime)          # Agent 1
        fields = await asyncio.to_thread(
            extraction.extract_record, ocr.text                    # Agent 2
        )
    except IngestionError as e:
        await tg.send_message(chat_id, f"⚠️ {e}")
        return
    except ExtractionError:
        await tg.send_message(
            chat_id,
            "I could read the document, but I couldn't find an expiry or "
            "renewal date on it. Could you send a clearer photo of the page "
            "with the dates?",
        )
        return
    except Exception:
        log.exception("Pipeline failure")
        await tg.send_message(
            chat_id,
            "Sorry, something went wrong while reading that. Please try again.",
        )
        return

    # Issuing country only applies to passports/licences (it drives their
    # renewal playbook). For those, prefer what the document says and fall
    # back to the user's stored default, labelling an assumed value so it
    # never looks like a read one. Other doc types carry no country.
    country_relevant = extraction.country_is_relevant(fields["doc_type"])
    read_country = fields.get("issuing_country") if country_relevant else None
    country = read_country or (
        user.get("default_country") if country_relevant else None
    )
    country_line = ""
    if country_relevant:
        if read_country:
            country_line = f"\nIssued in: {read_country}"
        elif country:
            country_line = f"\nIssued in: {country} (assumed from your usual)"
        else:
            country_line = "\nIssued in: not sure"
    record = {
        **fields,
        "issuing_country": country,
        "user_id": user["user_id"],
        "confidence": round(ocr.confidence, 3),
        "next_check_at": extraction.compute_next_check_at(
            fields["expiry_date"], fields["doc_type"]
        ),
    }
    summary = (
        f"*{record['doc_type'].title()}*, expires "
        f"*{record['expiry_date'].strftime('%d %b %Y')}*\n"
        f"Owner: {record.get('owner_name') or 'not shown'}\n"
        f"Ref: {record.get('reference_number') or 'not shown'}"
        f"{country_line}\n"
        f"(read via {ocr.engine.replace('_', ' ')}, "
        f"{record['confidence']:.0%} confident)"
    )

    # CONFIDENCE GATE (§6.3): low-quality extractions are never silently
    # persisted — the user must confirm first.
    if ocr.confidence < get_settings().confidence_threshold:
        pending_id = uuid.uuid4().hex[:10]
        PENDING_EXTRACTIONS[pending_id] = record
        if len(PENDING_EXTRACTIONS) > MAX_PENDING:
            PENDING_EXTRACTIONS.popitem(last=False)   # evict oldest
        await tg.send_message(
            chat_id,
            "🔍 Here's what I read, but I'm not fully confident. "
            "Mind double-checking it?\n\n" + summary,
            reply_markup=tg.confirm_keyboard(pending_id),
        )
        return

    doc_id = await asyncio.to_thread(db.write_document_record, record)
    await tg.send_message(chat_id, _saved_message(record, summary))
    log.info("Document %s saved for user %s", doc_id, user["user_id"])
    await _maybe_ask_country(chat_id, record, doc_id)


def _saved_message(record: dict[str, Any], summary: str = "") -> str:
    """Post-save confirmation. An already-expired document gets an honest
    'this has lapsed' response instead of a promise of future reminders."""
    if record["expiry_date"] < datetime.now(timezone.utc):
        days = (datetime.now(timezone.utc) - record["expiry_date"]).days
        return (
            ("⚠️ Saved, but heads up: this one *already expired* "
             + (f"{days} days ago" if days else "today") + ".\n\n")
            + summary
            + ("\n\n" if summary else "")
            + "The good news: it's fixable. Send /packet and I'll get you "
              "the renewal checklist and official link right away."
        )
    return (
        "✅ Saved!\n\n" + summary +
        ("\n\n" if summary else "") +
        "I'll nudge you well before it's due. Send /expiring anytime "
        "to see everything I'm tracking."
    )


async def _maybe_ask_country(chat_id: str, record: dict[str, Any], doc_id: str) -> None:
    """Country fallback (asked at most once per user): if neither the
    document nor the user profile tells us the issuing country, ask — the
    answer is stored on the document AND as the user's default. If the
    document DID tell us and the user has no default yet, learn it silently.
    Only relevant for passports/licences; other doc types have no country."""
    if not extraction.country_is_relevant(record["doc_type"]):
        return
    country = record.get("issuing_country")
    if country:
        user = await asyncio.to_thread(db.get_user, record["user_id"])
        if user and not user.get("default_country"):
            await asyncio.to_thread(
                db.update_user, record["user_id"], {"default_country": country}
            )
        return
    PENDING_COUNTRY[chat_id] = (doc_id, record["user_id"])
    if len(PENDING_COUNTRY) > MAX_PENDING:
        PENDING_COUNTRY.popitem(last=False)
    await tg.send_message(
        chat_id,
        "One quick question: which country issued this document? "
        "That way I can point you at the right official renewal site. "
        "(Just the name is fine, e.g. Singapore or UK.)",
    )


WELCOME_TEXT = (
    "👋 Hi, I'm *LifeKeeper*. I keep an eye on the expiry dates you'd "
    "rather not think about.\n\n"
    "📸 Send me a photo or PDF of a passport, driving licence, insurance "
    "policy, warranty, or membership card. I'll read it, remember the "
    "expiry date, and nudge you well before it sneaks up on you.\n\n"
    "You can also use:\n"
    "/expiring · see everything I'm tracking\n"
    "/packet · get a renewal checklist and calendar file\n"
    "/help · more about how I work"
)

HELP_TEXT = (
    "Here's how I work:\n\n"
    "📸 *Send a document* (photo or PDF, up to 10MB) and I'll read it, "
    "pull out the expiry date, and start watching it.\n\n"
    "⏰ *I remind you at the right moments*: an early heads-up, a final "
    "reminder, and an overdue alert. Never more than that.\n\n"
    "📦 */packet* gets you a renewal checklist, the official renewal site "
    "for the issuing country, and a calendar file you can import so the "
    "dates live in your own calendar too.\n\n"
    "🔒 I only store the extracted details, never the document itself, and "
    "I never fill in or submit anything on official websites. That part "
    "stays in your hands.\n\n"
    "Try /expiring to see what I'm tracking."
)

FALLBACK_TEXT = (
    "I'm not sure what you mean, but here's what I can do:\n"
    "📸 Send a document photo or PDF and I'll track its expiry\n"
    "/expiring · list your documents\n"
    "/packet · prepare a renewal\n"
    "/help · the full tour"
)


async def _handle_text(user: dict, chat_id: str, text: str) -> None:
    lower = text.lower().strip()

    if lower.startswith("/start"):
        await tg.send_message(chat_id, WELCOME_TEXT)
        return

    if lower.startswith("/help"):
        await tg.send_message(chat_id, HELP_TEXT)
        return

    if lower.startswith(("/packet", "/renew")) or "renewal packet" in lower:
        await _handle_renewal_request(user, chat_id)
        return

    if lower.startswith(("/expiring", "/list", "/status")) or any(
        k in lower for k in ("expire", "expiry", "documents", "list")
    ):
        reply = await scheduler.handle_query(user["user_id"], text)  # Agent 3 skill
        await tg.send_message(chat_id, reply)
        return

    # A pending "which country issued this?" question claims the next plain
    # message that isn't a command or a recognised request.
    if chat_id in PENDING_COUNTRY and not lower.startswith("/"):
        await _handle_country_answer(chat_id, text)
        return

    await tg.send_message(chat_id, FALLBACK_TEXT)


async def _handle_country_answer(chat_id: str, text: str) -> None:
    doc_id, user_id = PENDING_COUNTRY.pop(chat_id)
    country = extraction.normalize_country(text)
    if not country:
        PENDING_COUNTRY[chat_id] = (doc_id, user_id)
        await tg.send_message(chat_id, "Sorry, I didn't catch that. Which "
                                       "country issued the document?")
        return
    await asyncio.to_thread(db.update_document, doc_id, {"issuing_country": country})
    await asyncio.to_thread(db.update_user, user_id, {"default_country": country})
    await tg.send_message(
        chat_id,
        f"Got it, {country}! I'll assume the same for your future documents. "
        "If one is from somewhere else, just tell me.",
    )


async def _handle_renewal_request(user: dict, chat_id: str) -> None:
    """Conversational or /packet trigger → Agent 4 (Renewal Action)."""
    docs = await asyncio.to_thread(db.list_user_documents, user["user_id"])
    if not docs:
        await tg.send_message(
            chat_id,
            "I'm not tracking anything for you yet. Send me a document "
            "photo or PDF first and I'll take it from there!",
        )
        return
    docs.sort(key=lambda d: d["expiry_date"])   # most urgent first
    if len(docs) == 1:
        await _send_packet(chat_id, docs[0]["doc_id"])
        return
    await tg.send_message(
        chat_id,
        "Which document shall I prepare? (Soonest expiry first.)",
        reply_markup=tg.doc_picker_keyboard(docs),
    )


async def _send_packet(chat_id: str, doc_id: str) -> None:
    """Agent 4: checklist + official link + calendar file, then the
    lifecycle buttons. Nothing is submitted anywhere on the user's behalf."""
    await tg.send_message(chat_id, "📦 Putting your renewal packet together…")
    packet = await asyncio.to_thread(renewal.generate_renewal_packet, doc_id)

    checklist_text = "\n".join(
        f"{i}. {s}" for i, s in enumerate(packet["checklist"], 1)
    )
    if packet["official_url"]:
        url_line = f"\n\n🔗 Official renewal site: {packet['official_url']}"
    else:
        url_line = ("\n\nI don't have an official renewal link for this one, "
                    "so go through the issuer directly.")
    await tg.send_message(
        chat_id,
        f"Here's everything for your *{packet['doc_type']}* renewal "
        f"(expires {packet['expiry_date'].strftime('%d %b %Y')}).\n\n"
        f"*Your checklist:*\n{checklist_text}{url_line}\n\n"
        f"I'm attaching a calendar file with the key dates. Open it and "
        f"they'll drop straight into your calendar.\n\n"
        f"Tell me how it goes:",
        reply_markup=tg.lifecycle_keyboard(doc_id),
    )
    await tg.send_document(
        chat_id,
        f"renew-{packet['doc_type']}.ics",
        packet["calendar_ics"].encode("utf-8"),
        caption="📅 Key renewal dates for your calendar",
        mime_type="text/calendar",
    )


async def _handle_callback(cb: dict[str, Any]) -> None:
    """Inline keyboard taps: document lifecycle (renewed/snooze/stop),
    packet picker, low-confidence confirm, plus legacy approve/cancel."""
    data = cb.get("data", "")
    chat_id = str(cb["message"]["chat"]["id"])
    action, _, payload = data.partition(":")

    if action == "packet":
        await tg.answer_callback(cb["id"])
        await _send_packet(chat_id, payload)

    elif action == "renewed":
        await asyncio.to_thread(
            db.update_document, payload, {"status": "renewed"}
        )
        await tg.answer_callback(cb["id"], "Congrats! 🎉")
        await tg.send_message(
            chat_id,
            "🎉 Nice work! I've closed that one out, so no more reminders "
            "for it. When the new document arrives, send me a photo and "
            "I'll start watching it.",
        )

    elif action == "snooze":
        ok = await asyncio.to_thread(scheduler.snooze_document, payload)
        await tg.answer_callback(cb["id"], "Snoozed 😴" if ok else "Hmm, not found")
        if ok:
            await tg.send_message(
                chat_id, "😴 No problem. I'll check back in about a week."
            )

    elif action == "stop":
        await asyncio.to_thread(
            db.update_document, payload, {"status": "archived"}
        )
        await tg.answer_callback(cb["id"], "Stopped")
        await tg.send_message(
            chat_id,
            "👍 Done, I've stopped tracking that document. If you change "
            "your mind, just send it to me again.",
        )

    elif action == "confirmdoc":
        record = PENDING_EXTRACTIONS.pop(payload, None)
        await tg.answer_callback(cb["id"])
        if record:
            doc_id = await asyncio.to_thread(db.write_document_record, record)
            await tg.send_message(chat_id, _saved_message(record))
            await _maybe_ask_country(chat_id, record, doc_id)
        else:
            await tg.send_message(
                chat_id,
                "That confirmation expired. Please send the document again.",
            )

    elif action == "discarddoc":
        PENDING_EXTRACTIONS.pop(payload, None)
        await tg.answer_callback(cb["id"], "Discarded")
        await tg.send_message(chat_id, "🗑 Discarded. Nothing was saved.")

    # Legacy approve/cancel buttons (pre-lifecycle messages may still carry
    # them) — also the MCP approve_action contract, resolved via Telegram.
    elif action == "approve":
        ok = await asyncio.to_thread(renewal.approve_action, payload)
        await tg.answer_callback(cb["id"], "Approved ✅" if ok else "Already handled")
        if ok:
            await tg.send_message(
                chat_id,
                "✅ Approved. Follow the checklist and the official link to "
                "complete your renewal. I never submit anything to "
                "third-party portals on your behalf, and I'll keep tracking "
                "this document meanwhile.",
            )

    elif action == "cancel":
        ok = await asyncio.to_thread(renewal.cancel_action, payload)
        await tg.answer_callback(cb["id"], "Cancelled" if ok else "Already handled")
        if ok:
            await tg.send_message(chat_id, "❌ Cancelled. No action taken.")


# ============================================== Scheduler cron target ======

@app.post("/tasks/run-scheduler")
async def run_scheduler(
    x_scheduler_token: str = Header(default=""),
) -> dict[str, int]:
    """Fired daily by Cloud Scheduler (deploy.sh) — and manually during the
    demo, because judges can't wait 90 days for a tier crossing:

        curl -X POST $SERVICE_URL/tasks/run-scheduler \
             -H "X-Scheduler-Token: $SCHEDULER_TOKEN"
    """
    if not hmac.compare_digest(x_scheduler_token, get_settings().scheduler_token):
        raise HTTPException(status_code=403)
    return await scheduler.run_scheduler_pass()  # Agent 3: Scheduler


# ===================================================== MCP tool routes =====
# Design Doc §5 — the pipeline service doubles as an MCP-style tool server
# so ADK agents (see app/adk_agents.py) can call LifeKeeper capabilities as
# tools. JSON-over-HTTP tool endpoints matching the §5.1 contract.
#
# These return PII and resolve the HITL gate, so they require the shared
# internal token (same secret as the scheduler endpoint) — the Cloud Run
# service itself is public for the Telegram webhook.

def _require_api_token(x_api_token: str) -> None:
    if not hmac.compare_digest(x_api_token, get_settings().scheduler_token):
        raise HTTPException(status_code=403)


@app.post("/mcp/tools/get_records")
async def mcp_get_records(
    body: dict[str, Any], x_api_token: str = Header(default="")
) -> dict[str, Any]:
    """MCP tool `get_records`: query a user's document memory."""
    _require_api_token(x_api_token)
    user_id = body.get("user_id", "")
    if not user_id:
        raise HTTPException(422, "user_id required")
    docs = await asyncio.to_thread(db.list_user_documents, user_id)
    if body.get("doc_type"):
        docs = [d for d in docs if d["doc_type"] == body["doc_type"]]
    for d in docs:  # timestamps → ISO strings for JSON transport
        d["expiry_date"] = d["expiry_date"].isoformat()
        d.pop("next_check_at", None)
        d.pop("created_at", None)
    return {"records": docs}


@app.post("/mcp/tools/generate_packet")
async def mcp_generate_packet(
    body: dict[str, Any], x_api_token: str = Header(default="")
) -> dict[str, Any]:
    """MCP tool `generate_packet`: renewal packet for a doc_id."""
    _require_api_token(x_api_token)
    doc_id = body.get("doc_id", "")
    if not doc_id:
        raise HTTPException(422, "doc_id required")
    try:
        return await asyncio.to_thread(renewal.generate_renewal_packet, doc_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/mcp/tools/approve_action")
async def mcp_approve_action(
    body: dict[str, Any], x_api_token: str = Header(default="")
) -> dict[str, bool]:
    """MCP tool `approve_action`: resolve the HITL gate. Exposed for the web
    UI path; the Telegram inline keyboard uses the same underlying skill."""
    _require_api_token(x_api_token)
    action_id = body.get("action_id", "")
    if not action_id:
        raise HTTPException(422, "action_id required")
    approved = await asyncio.to_thread(renewal.approve_action, action_id)
    return {"approved": approved}
