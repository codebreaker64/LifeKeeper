"""Shared test config: dummy env vars so Settings() can construct without a
real .env or GCP credentials. Real environment variables (if you have them
set) still take precedence — setdefault never overwrites."""

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("DOCAI_PROCESSOR_ID", "test-processor")
os.environ.setdefault("SCHEDULER_TOKEN", "test-scheduler-token")
os.environ.setdefault("GCP_PROJECT_ID", "test-project")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")  # for the emulator
