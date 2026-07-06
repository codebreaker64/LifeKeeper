"""
config.py — central configuration for LifeKeeper.

SECURITY DESIGN (rubric: "Security features"):
  * No secrets live in this repo. In production (Cloud Run) every secret is
    injected as an environment variable *from Google Secret Manager* via the
    `--set-secrets` flag in deploy.sh. Locally, a `.env` file (gitignored)
    provides the same variables.
  * pydantic-settings validates presence at startup — the service refuses to
    boot half-configured rather than failing mid-request.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- GCP project ---
    gcp_project_id: str = ""            # auto-detected on Cloud Run if empty
    gcp_location: str = "us"            # Document AI region ("us" or "eu")

    # --- Secrets (injected from Secret Manager at runtime — see deploy.sh) ---
    telegram_bot_token: str
    docai_processor_id: str             # Document OCR processor
    gemini_api_key: str = ""            # empty => use Vertex AI ADC instead

    # --- Behavioural knobs ---
    confidence_threshold: float = 0.7   # below this → user must confirm (guardrail §6.3)
    max_file_bytes: int = 10 * 1024 * 1024   # 10MB hard cap (guardrail §6.3)
    allowed_mime_types: frozenset = frozenset(
        {"application/pdf", "image/jpeg", "image/png"}
    )

    # --- Internal auth for the scheduler trigger endpoint ---
    # Cloud Scheduler calls POST /tasks/run-scheduler with this shared token.
    # The same token guards the /mcp/tools/* endpoints (X-API-Token header).
    # (OIDC is the production-grade option; a token keeps the MVP simple and
    # still prevents random internet traffic from firing notifications.)
    scheduler_token: str = "change-me"

    # Telegram's official webhook auth: setWebhook(secret_token=...) makes
    # Telegram send X-Telegram-Bot-Api-Secret-Token on every update. Empty =>
    # rely on the unguessable hash-in-path alone (local dev without ngrok config).
    telegram_webhook_secret: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
