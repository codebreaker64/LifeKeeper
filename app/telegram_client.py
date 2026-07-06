"""
telegram_client.py — thin async wrapper over the Telegram Bot API.

This is the only user-facing channel in the MVP. The `notify()` tool in the
Scheduler Agent keeps a channel-agnostic signature so email (SendGrid) and
SMS (Twilio) can slot in later without touching agent logic (Design Doc §4.3).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .config import get_settings

log = logging.getLogger("lifekeeper.telegram")


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{get_settings().telegram_bot_token}/{method}"


def _file_url(file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{get_settings().telegram_bot_token}/{file_path}"


def _split_text(text: str, limit: int = 4000) -> list[str]:
    """Telegram rejects messages over 4096 chars. Split at the last newline
    before the limit so Markdown entities (which never span lines in our
    messages) aren't cut in half."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


async def send_message(
    chat_id: str,
    text: str,
    reply_markup: Optional[dict[str, Any]] = None,
) -> None:
    # Long content is split; the keyboard (if any) rides on the final chunk.
    chunks = _split_text(text)
    for chunk in chunks[:-1]:
        await _post_message(chat_id, chunk, None)
    await _post_message(chat_id, chunks[-1], reply_markup)


async def _post_message(
    chat_id: str,
    text: str,
    reply_markup: Optional[dict[str, Any]],
) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(_api("sendMessage"), json=payload)
        if r.status_code == 400:
            payload.pop("parse_mode", None)   # retry without Markdown
            r = await client.post(_api("sendMessage"), json=payload)
        if r.status_code != 200:
            log.error("sendMessage failed %s: %s", r.status_code, r.text)

async def send_document(
    chat_id: str,
    filename: str,
    data: bytes,
    caption: str = "",
    mime_type: str = "application/octet-stream",
) -> None:
    """Send a file attachment (e.g. the renewal packet's .ics calendar)."""
    payload: dict[str, Any] = {"chat_id": chat_id}
    if caption:
        payload["caption"] = caption
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            _api("sendDocument"),
            data=payload,
            files={"document": (filename, data, mime_type)},
        )
        if r.status_code != 200:
            log.error("sendDocument failed %s: %s", r.status_code, r.text)


BOT_COMMANDS = [
    {"command": "start", "description": "What LifeKeeper does"},
    {"command": "expiring", "description": "See everything I'm tracking"},
    {"command": "packet", "description": "Get a renewal checklist + calendar file"},
    {"command": "help", "description": "How to use me"},
]


async def set_my_commands() -> bool:
    """Register the command menu with Telegram (persists server-side, so a
    one-off call is enough — see scripts/set_commands.py and deploy.sh)."""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(_api("setMyCommands"), json={"commands": BOT_COMMANDS})
        if r.status_code != 200:
            log.error("setMyCommands failed %s: %s", r.status_code, r.text)
        return r.status_code == 200


async def answer_callback(callback_query_id: str, text: str = "") -> None:
    """Acknowledge an inline-keyboard tap so the Telegram client stops the
    spinner on the button."""
    async with httpx.AsyncClient(timeout=20) as client:
        await client.post(
            _api("answerCallbackQuery"),
            json={"callback_query_id": callback_query_id, "text": text},
        )


async def fetch_file(file_id: str) -> tuple[bytes, str]:
    """Ingestion Agent skill `fetch_telegram_file` (Design Doc §4.1).

    Two-step Bot API dance: getFile → download from the Telegram CDN.
    Returns (bytes, best-guess mime type from the file extension).
    """
    async with httpx.AsyncClient(timeout=60) as client:
        meta = await client.get(_api("getFile"), params={"file_id": file_id})
        meta.raise_for_status()
        file_path: str = meta.json()["result"]["file_path"]

        blob = await client.get(_file_url(file_path))
        blob.raise_for_status()

    ext = file_path.rsplit(".", 1)[-1].lower()
    mime = {
        "pdf": "application/pdf",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
    }.get(ext, "application/octet-stream")
    return blob.content, mime


def lifecycle_keyboard(doc_id: str) -> dict[str, Any]:
    """Document lifecycle buttons, shown on reminders and renewal packets.
    Each tap changes how LifeKeeper behaves in the future: renewed and stop
    end the reminders, snooze re-arms the current one a week out. None of
    them touch anything outside LifeKeeper. callback_data round-trips
    through Telegram back to our webhook."""
    return {
        "inline_keyboard": [
            [{"text": "🎉 I've renewed it", "callback_data": f"renewed:{doc_id}"}],
            [{"text": "⏰ Remind me in a week", "callback_data": f"snooze:{doc_id}"}],
            [{"text": "🙈 Stop tracking this", "callback_data": f"stop:{doc_id}"}],
        ]
    }


def doc_picker_keyboard(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """One button per tracked document, for picking a /packet target."""
    return {
        "inline_keyboard": [
            [{
                "text": f"{d['doc_type'].title()} · expires "
                        f"{d['expiry_date']:%d %b %Y}",
                "callback_data": f"packet:{d['doc_id']}",
            }]
            for d in docs[:10]
        ]
    }


def confirm_keyboard(pending_id: str) -> dict[str, Any]:
    """Yes / No keyboard for the low-confidence extraction gate (§6.3)."""
    return {
        "inline_keyboard": [[
            {"text": "✅ Looks right, save it", "callback_data": f"confirmdoc:{pending_id}"},
            {"text": "🗑 Discard",              "callback_data": f"discarddoc:{pending_id}"},
        ]]
    }
