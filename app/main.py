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
import logging
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from . import firestore_db as db
from . import telegram_client as tg
from .agents import extraction, renewal, scheduler
from .agents.ingestion import IngestionError, ingest
from .config import get_settings
from collections import OrderedDict

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lifekeeper.main")

app = FastAPI(title="LifeKeeper", version="1.0.0")

# In-memory holding area for low-confidence extractions awaiting the user's
# confirm tap. Acceptable for MVP (single Cloud Run instance during demo);
# move to a Firestore `pending_extractions` collection for multi-instance.
PENDING_EXTRACTIONS: dict[str, dict[str, Any]] = {}

# LRU of processed Telegram update_ids — Telegram redelivers on error/timeout;
# never process the same update twice.
SEEN_UPDATES: OrderedDict[int, None] = OrderedDict()


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
    if token_hash != _token_hash():
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
        await _handle_callback(update["callback_query"])
        return {"ok": True}

    message = update.get("message") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    if not chat_id:
        return {"ok": True}

    user = db.get_or_create_user(chat_id)

    file_id = _extract_file_id(message)
    if file_id:
        # ACK Telegram immediately; run the heavy Document AI / Gemini
        # pipeline in the background so a slow or crashing pipeline can
        # never cause a webhook timeout → redelivery storm.
        asyncio.create_task(_handle_document(user, chat_id, file_id))
        return {"ok": True}


    text = (message.get("text") or "").strip()
    try:
        await _handle_text(user, chat_id, text)
    except Exception:
        # A handler crash must never 500 the webhook (Telegram would
        # redeliver) — log it and tell the user instead of going silent.
        log.exception("Text handler failed for chat %s", chat_id)
        await tg.send_message(chat_id, "Sorry — something went wrong. Please try again.")
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
        ocr = ingest(data, mime)                      # Agent 1: Ingestion
        fields = extraction.extract_record(ocr.text)  # Agent 2: Extraction
    except IngestionError as e:
        await tg.send_message(chat_id, f"⚠️ {e}")
        return
    except ValueError:
        await tg.send_message(
            chat_id,
            "I read the document but couldn't find an expiry or renewal "
            "date. Could you send a clearer photo of the page with the dates?",
        )
        return
    except Exception:
        log.exception("Pipeline failure")
        await tg.send_message(
            chat_id, "Something went wrong reading that document — please try again."
        )
        return

    record = {
        **fields,
        "user_id": user["user_id"],
        "confidence": round(ocr.confidence, 3),
        "next_check_at": extraction.compute_next_check_at(
            fields["expiry_date"], fields["doc_type"]
        ),
    }
    summary = (
        f"*{record['doc_type'].title()}* — expires "
        f"*{record['expiry_date'].strftime('%d %b %Y')}*\n"
        f"Owner: {record.get('owner_name') or '—'}\n"
        f"Ref: {record.get('reference_number') or '—'}\n"
        f"(read via {ocr.engine.replace('_', ' ')}, confidence {record['confidence']:.0%})"
    )

    # CONFIDENCE GATE (§6.3): low-quality extractions are never silently
    # persisted — the user must confirm first.
    if ocr.confidence < get_settings().confidence_threshold:
        pending_id = uuid.uuid4().hex[:10]
        PENDING_EXTRACTIONS[pending_id] = record
        await tg.send_message(
            chat_id,
            "🔍 I read this, but confidence is low — please double-check:\n\n"
            + summary,
            reply_markup=tg.confirm_keyboard(pending_id),
        )
        return

    doc_id = db.write_document_record(record)
    await tg.send_message(
        chat_id,
        "✅ Saved!\n\n" + summary +
        f"\n\nI'll remind you when it's time to renew. "
        f"Ask me *what expires soon?* anytime.",
    )
    log.info("Document %s saved for user %s", doc_id, user["user_id"])


async def _handle_text(user: dict, chat_id: str, text: str) -> None:
    lower = text.lower()

    if lower.startswith("/start"):
        await tg.send_message(
            chat_id,
            "👋 Hi, I'm *LifeKeeper* — your life-admin renewal agent.\n\n"
            "📸 Send me a photo or PDF of a passport, driving licence, "
            "insurance policy, warranty, or membership card.\n\n"
            "I'll extract the expiry date, remember it forever, remind you "
            "in good time, and prepare your renewal packet when you ask.\n\n"
            "Try: *what expires soon?*",
        )
        return

    if "renewal packet" in lower or lower.startswith("/renew"):
        await _handle_renewal_request(user, chat_id)
        return

    if any(k in lower for k in ("expire", "expiry", "documents", "list", "/status")):
        reply = await scheduler.handle_query(user["user_id"], text)  # Agent 3 skill
        await tg.send_message(chat_id, reply)
        return

    await tg.send_message(
        chat_id,
        "I can help with:\n"
        "📸 Send a document photo/PDF — I'll track its expiry\n"
        "📋 *what expires soon?* — list your documents\n"
        "📦 *generate my renewal packet* — prepare your next renewal",
    )


async def _handle_renewal_request(user: dict, chat_id: str) -> None:
    """Conversational trigger → Agent 4 (Renewal Action) → HITL keyboard."""
    docs = db.list_user_documents(user["user_id"])
    if not docs:
        await tg.send_message(
            chat_id, "You have no tracked documents yet — send me one first!"
        )
        return
    # Most urgent document first (soonest expiry).
    docs.sort(key=lambda d: d["expiry_date"])
    target = docs[0]

    await tg.send_message(chat_id, "📦 Preparing your renewal packet…")
    packet = renewal.generate_renewal_packet(target["doc_id"])  # Agent 4

    checklist_text = "\n".join(
        f"{i}. {s}" for i, s in enumerate(packet["checklist"], 1)
    )
    url_line = (
        f"\n🔗 Official link: {packet['official_url']}" if packet["official_url"] else ""
    )
    await tg.send_message(
        chat_id,
        f"📦 *Renewal packet ready* for your {target['doc_type']} "
        f"(expires {target['expiry_date'].strftime('%d %b %Y')}):\n\n"
        f"📄 [Download packet PDF]({packet['packet_url']})\n\n"
        f"*Checklist:*\n{checklist_text}{url_line}\n\n"
        f"⚠️ Nothing happens without your say-so — approve to proceed:",
        reply_markup=tg.hitl_keyboard(packet["action_id"]),  # ← HITL GATE
    )


async def _handle_callback(cb: dict[str, Any]) -> None:
    """Inline keyboard taps: HITL approve/cancel + low-confidence confirm."""
    data = cb.get("data", "")
    chat_id = str(cb["message"]["chat"]["id"])
    action, _, payload = data.partition(":")

    if action == "approve":
        ok = renewal.approve_action(payload)
        await tg.answer_callback(cb["id"], "Approved ✅" if ok else "Already handled")
        if ok:
            await tg.send_message(
                chat_id,
                "✅ *Approved.* Status → awaiting_review.\n"
                "Follow the checklist and the official link to complete your "
                "renewal — I never submit anything to third-party portals on "
                "your behalf. I'll keep tracking this document meanwhile.",
            )

    elif action == "cancel":
        ok = renewal.cancel_action(payload)
        await tg.answer_callback(cb["id"], "Cancelled" if ok else "Already handled")
        if ok:
            await tg.send_message(chat_id, "❌ Cancelled — no action taken.")

    elif action == "confirmdoc":
        record = PENDING_EXTRACTIONS.pop(payload, None)
        await tg.answer_callback(cb["id"])
        if record:
            db.write_document_record(record)
            await tg.send_message(chat_id, "✅ Saved — I'll watch this one for you.")
        else:
            await tg.send_message(chat_id, "That confirmation expired — please resend the document.")

    elif action == "discarddoc":
        PENDING_EXTRACTIONS.pop(payload, None)
        await tg.answer_callback(cb["id"], "Discarded")
        await tg.send_message(chat_id, "🗑 Discarded — nothing was saved.")


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
    if x_scheduler_token != get_settings().scheduler_token:
        raise HTTPException(status_code=403)
    return await scheduler.run_scheduler_pass()  # Agent 3: Scheduler


# ===================================================== MCP tool routes =====
# Design Doc §5 — the pipeline service doubles as an MCP-style tool server
# so ADK agents (see app/adk_agents.py) can call LifeKeeper capabilities as
# tools. JSON-over-HTTP tool endpoints matching the §5.1 contract.

@app.post("/mcp/tools/get_records")
async def mcp_get_records(body: dict[str, Any]) -> dict[str, Any]:
    """MCP tool `get_records`: query a user's document memory."""
    user_id = body.get("user_id", "")
    if not user_id:
        raise HTTPException(422, "user_id required")
    docs = db.list_user_documents(user_id)
    if body.get("doc_type"):
        docs = [d for d in docs if d["doc_type"] == body["doc_type"]]
    for d in docs:  # timestamps → ISO strings for JSON transport
        d["expiry_date"] = d["expiry_date"].isoformat()
        d.pop("next_check_at", None)
        d.pop("created_at", None)
    return {"records": docs}


@app.post("/mcp/tools/generate_packet")
async def mcp_generate_packet(body: dict[str, Any]) -> dict[str, Any]:
    """MCP tool `generate_packet`: renewal packet for a doc_id."""
    doc_id = body.get("doc_id", "")
    if not doc_id:
        raise HTTPException(422, "doc_id required")
    try:
        return renewal.generate_renewal_packet(doc_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/mcp/tools/approve_action")
async def mcp_approve_action(body: dict[str, Any]) -> dict[str, bool]:
    """MCP tool `approve_action`: resolve the HITL gate. Exposed for the web
    UI path; the Telegram inline keyboard uses the same underlying skill."""
    action_id = body.get("action_id", "")
    if not action_id:
        raise HTTPException(422, "action_id required")
    return {"approved": renewal.approve_action(action_id)}
