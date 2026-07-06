"""
telegram_client.py — thin async wrapper over the Telegram Bot API.

This is the only user-facing channel in the MVP. The `notify()` tool in the
Scheduler Agent keeps a channel-agnostic signature so email (SendGrid) and
SMS (Twilio) can slot in later without touching agent logic (Design Doc §4.3).
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from .config import get_settings


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{get_settings().telegram_bot_token}/{method}"


def _file_url(file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{get_settings().telegram_bot_token}/{file_path}"


async def send_message(
    chat_id: str,
    text: str,
    reply_markup: Optional[dict[str, Any]] = None,
) -> None:
    # Telegram rejects messages over 4096 chars ("message is too long").
    # Split long content; the keyboard (if any) rides on the final chunk.
    if len(text) > 4000:
        await send_message(chat_id, text[:4000])
        return await send_message(chat_id, text[4000:], reply_markup)    
    import logging
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
            logging.getLogger("lifekeeper.telegram").error(
                "sendMessage failed %s: %s", r.status_code, r.text
            )

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


def hitl_keyboard(action_id: str) -> dict[str, Any]:
    """Approve / Cancel inline keyboard — the Human-in-the-Loop gate.
    callback_data round-trips through Telegram and back to our webhook."""
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve:{action_id}"},
            {"text": "❌ Cancel",  "callback_data": f"cancel:{action_id}"},
        ]]
    }


def confirm_keyboard(pending_id: str) -> dict[str, Any]:
    """Yes / No keyboard for the low-confidence extraction gate (§6.3)."""
    return {
        "inline_keyboard": [[
            {"text": "✅ Looks right — save it", "callback_data": f"confirmdoc:{pending_id}"},
            {"text": "🗑 Discard",               "callback_data": f"discarddoc:{pending_id}"},
        ]]
    }
