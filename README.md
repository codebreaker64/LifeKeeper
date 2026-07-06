# 🗂 LifeKeeper — AI Life-Admin Renewal Agent

*Kaggle × Google AI Agents Capstone · Track: Concierge Agents / Agents for Good*

**One-liner:** A four-agent pipeline that ingests life documents via Telegram, extracts renewal deadlines with Google Document AI, stores them in long-term Firestore memory, and proactively initiates renewal flows — with human-in-the-loop safety gates on every action.

---

## The Problem

People routinely miss passport renewals, insurance lapses, and voided warranties — not from carelessness, but because expiry dates are scattered across physical documents, emails, and PDFs that are never revisited until it is too late. No single system watches all of them proactively.

## The Solution

Photograph any life document and send it to LifeKeeper on Telegram. The system:

1. **Reads it** — Google Document AI OCR (Gemini vision fallback)
2. **Understands it** — structured extraction: type, expiry, owner, reference number
3. **Remembers it forever** — Firestore long-term memory across sessions
4. **Watches the horizon** — a daily scheduler reasons weeks/months ahead using per-document urgency tiers
5. **Acts when you're ready** — generates a renewal packet (PDF + checklist + official URL), gated behind explicit human approval

## Architecture

```
                         ┌────────────────────────────────────────────────┐
 Telegram photo/PDF ────▶│  Cloud Run: lifekeeper (FastAPI)               │
                         │                                                │
                         │  ① Ingestion Agent                             │
                         │     validate → Document AI OCR → confidence    │
                         │              ↓ (fallback: Gemini vision)       │
                         │  ② Extraction Agent                            │
                         │     Gemini structured JSON → typed record      │
                         │     confidence < 0.7 → user confirm gate       │
                         │              ↓                                 │
                         │        Firestore (long-term memory)            │
                         │     documents / users / renewal_actions        │
                         │              ↑                                 │
 Cloud Scheduler ───────▶│  ③ Reminder & Scheduler Agent                  │
 (daily 07:00 UTC)       │     WHERE next_check_at <= now()               │
                         │     tier-crossing logic → notify() → Telegram  │
                         │              ↓  "generate my renewal packet"   │
                         │  ④ Renewal Action Agent                        │
                         │     packet PDF → GCS signed URL → checklist    │
                         │     ⚠ HITL GATE: Approve/Cancel inline keys    │
                         └────────────────────────────────────────────────┘
                              MCP tool endpoints: /mcp/tools/{get_records,
                              generate_packet, approve_action} — consumed
                              by the ADK agents in app/adk_agents.py
```

### The four agents

| # | Agent | Module | Responsibility |
|---|-------|--------|----------------|
| 1 | Ingestion | `app/agents/ingestion.py` | File guardrails, Document AI OCR, Gemini vision fallback |
| 2 | Extraction | `app/agents/extraction.py` | Structured record extraction, urgency windows, `next_check_at` |
| 3 | Reminder & Scheduler | `app/agents/scheduler.py` | Long-horizon tier-crossing reminders, conversational queries |
| 4 | Renewal Action | `app/agents/renewal.py` | Packet PDF + checklist + official URL, HITL approval gate |

The same agents are also defined as a **Google ADK multi-agent system** (root agent + specialist sub-agents with tools) in `app/adk_agents.py` — runnable with `adk web app/`.

### Key design decisions

- **Tier-crossing notifications** — `last_notified_tier` means the daily cron notifies only when urgency *escalates* (`null → early_warning → final_reminder → overdue`), never spamming.
- **`next_check_at` indexing** — the scheduler queries `WHERE next_check_at <= now()` (composite index) rather than scanning every document. Scales.
- **Confidence gate** — Document AI confidence < 0.7 forces user confirmation before anything is persisted. Low-quality extractions are never silently saved.
- **HITL gate** — renewal actions stay `drafted` until the user taps **Approve**. The agent **never automates third-party portals** — no browser sessions, no form submissions on government or insurer sites. Responsible agentic design by construction.

## Security (Design Doc §6)

- **Secrets**: all in Google Secret Manager, injected via Cloud Run `--set-secrets`. Nothing in the repo — verify with `git grep -iE "api_key|token" -- ':!*.md'`.
- **Least-privilege IAM**: single service account with only `datastore.user`, `documentai.apiUser`, `secretmanager.secretAccessor`, `storage.objectAdmin`.
- **Input validation**: MIME whitelist (PDF/JPEG/PNG), 10MB hard cap, enforced before any paid API call.
- **Webhook security**: Telegram webhook path embeds a SHA-256 hash of the bot token — unguessable URL. Scheduler endpoint requires a shared secret header.
- **PII**: Firestore stores extracted fields only, never raw document bytes. Packet PDFs live in GCS with 7-day signed URLs and 30-day auto-delete.

## Setup

### Prerequisites
- GCP project with billing enabled, `gcloud` CLI authenticated
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- Gemini API key from [AI Studio](https://aistudio.google.com/) (or omit to use Vertex AI ADC)

### Deploy (one script)

```bash
# 1. In GCP console: Document AI → Create Processor → "Document OCR" (region: us)
export DOCAI_PROCESSOR_ID=<processor-id>

# 2. Secrets (never committed)
export TELEGRAM_BOT_TOKEN=<from BotFather>
export GEMINI_API_KEY=<from AI Studio>
export SCHEDULER_TOKEN=$(openssl rand -hex 16)

# 3. Bootstrap everything: APIs, Firestore + index, GCS bucket, IAM,
#    Secret Manager, Cloud Run deploy, Telegram webhook, Cloud Scheduler cron
./deploy.sh
```

### Try it
1. Open your bot in Telegram → `/start`
2. Send a photo of a passport / licence / insurance document
3. Ask: `what expires soon?`
4. Say: `generate my renewal packet` → receive PDF + checklist → tap **Approve**

### Demo the long-horizon scheduler (no 90-day wait)
```bash
python scripts/seed_demo.py <your_telegram_chat_id>   # plants near-boundary docs
curl -X POST $SERVICE_URL/tasks/run-scheduler -H "X-Scheduler-Token: $SCHEDULER_TOKEN"
# → Telegram receives an early_warning and a final_reminder, exactly once.
# Run the curl again: zero notifications (tier-crossing invariant).
```

### Run locally
```bash
pip install -r requirements.txt
cp .env.example .env          # fill in values
uvicorn app.main:app --reload # + `ngrok http 8000` for the Telegram webhook
python -m pytest tests/ -q    # offline logic tests (8 passing)
adk web app/                  # explore the ADK multi-agent system interactively
```

## Course Concepts Demonstrated

| Concept | Where |
|---|---|
| Multi-agent system (ADK) | `app/adk_agents.py` (root + sub-agents), `app/agents/*` (pipeline) |
| MCP server | `/mcp/tools/*` endpoints in `app/main.py` (Design Doc §5 contract) |
| Security features | Secret Manager, IAM, guardrails — see Security section + `deploy.sh` |
| Deployability | One-script Cloud Run deploy + Cloud Scheduler cron (`deploy.sh`) |
| Agent skills | Each agent module maps 1:1 to the skills tables in the design doc §4 |

## Project Structure

```
lifekeeper/
├── app/
│   ├── main.py            # FastAPI: webhook, HITL callbacks, cron, MCP tools
│   ├── adk_agents.py      # ADK multi-agent definitions (root + specialists)
│   ├── config.py          # settings; secrets injected from Secret Manager
│   ├── firestore_db.py    # long-term memory: users/documents/renewal_actions
│   ├── telegram_client.py # Bot API wrapper + inline keyboards (HITL)
│   └── agents/
│       ├── ingestion.py   # ① guardrails + Document AI + Gemini fallback
│       ├── extraction.py  # ② structured extraction + urgency windows
│       ├── scheduler.py   # ③ tier-crossing reminders, long-horizon memory
│       └── renewal.py     # ④ packet PDF + GCS + HITL gate
├── scripts/seed_demo.py   # plant demo docs at tier boundaries
├── tests/test_core_logic.py
├── deploy.sh              # full GCP bootstrap + deploy + webhook + cron
├── Dockerfile
└── requirements.txt
```

## Limitations & Roadmap

- Pre-filled official forms (DS-82, DVLA D1) — packet is currently a generated summary + checklist (`template_matched: false`, surfaced honestly to the user)
- Email (SendGrid) / SMS (Twilio) channels — `notify()` is channel-agnostic by design; Telegram only for now
- Web upload path — the ingestion endpoint is source-agnostic and ready for it
