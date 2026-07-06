#!/usr/bin/env bash
# =============================================================================
# deploy.sh — bootstrap GCP + deploy LifeKeeper to Cloud Run in one pass.
#
# Prerequisites:
#   * gcloud CLI authenticated:  gcloud auth login && gcloud config set project <ID>
#   * These env vars exported BEFORE running (values never touch the repo):
#       export TELEGRAM_BOT_TOKEN=...     # from @BotFather
#       export GEMINI_API_KEY=...         # from AI Studio (or leave unset for Vertex ADC)
#       export SCHEDULER_TOKEN=$(openssl rand -hex 16)
#
# Idempotent-ish: safe to re-run; existing resources print errors you can ignore.
# =============================================================================
set -euo pipefail

PROJECT_ID=$(gcloud config get-value project)
REGION="us-central1"
DOCAI_LOCATION="us"
SERVICE="lifekeeper"
BUCKET="${PROJECT_ID}-lifekeeper-packets"
SA="lifekeeper-sa"
SA_EMAIL="${SA}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "▸ 1/8 Enabling APIs…"
gcloud services enable \
  run.googleapis.com firestore.googleapis.com documentai.googleapis.com \
  secretmanager.googleapis.com storage.googleapis.com \
  cloudscheduler.googleapis.com cloudbuild.googleapis.com

echo "▸ 2/8 Firestore database (native mode)…"
gcloud firestore databases create --location="${REGION}" || true

echo "▸ 3/8 Composite index (status + next_check_at) for the scheduler query…"
gcloud firestore indexes composite create \
  --collection-group=documents \
  --field-config field-path=status,order=ascending \
  --field-config field-path=next_check_at,order=ascending || true

echo "▸ 4/8 GCS bucket for renewal packets (auto-delete after 30 days)…"
gcloud storage buckets create "gs://${BUCKET}" --location="${REGION}" || true
cat > /tmp/lifecycle.json <<'EOF'
{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}
EOF
gcloud storage buckets update "gs://${BUCKET}" --lifecycle-file=/tmp/lifecycle.json

echo "▸ 5/8 Service account + least-privilege IAM (Design Doc §6.2)…"
gcloud iam service-accounts create "${SA}" --display-name="LifeKeeper" || true
for ROLE in roles/datastore.user roles/documentai.apiUser \
            roles/secretmanager.secretAccessor roles/storage.objectAdmin \
            roles/iam.serviceAccountTokenCreator; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" --role="${ROLE}" --quiet >/dev/null
done
# (serviceAccountTokenCreator is needed for GCS v4 signed URLs from Cloud Run)

echo "▸ 6/8 Document AI processor (Document OCR)…"
echo "  ⚠ Create manually in console → Document AI → Create Processor →"
echo "     'Document OCR' (region: ${DOCAI_LOCATION}), then:"
echo "     export DOCAI_PROCESSOR_ID=<the processor id>"
: "${DOCAI_PROCESSOR_ID:?Set DOCAI_PROCESSOR_ID before continuing}"

echo "▸ 7/8 Secrets → Secret Manager (never in the repo)…"
create_secret() {
  printf '%s' "$2" | gcloud secrets create "$1" --data-file=- 2>/dev/null \
    || printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=-
}
create_secret TELEGRAM_BOT_TOKEN   "${TELEGRAM_BOT_TOKEN:?}"
create_secret GEMINI_API_KEY       "${GEMINI_API_KEY:-}"
create_secret DOCAI_PROCESSOR_ID   "${DOCAI_PROCESSOR_ID}"
create_secret SCHEDULER_TOKEN      "${SCHEDULER_TOKEN:?}"

echo "▸ 8/8 Deploying to Cloud Run…"
gcloud run deploy "${SERVICE}" \
  --source . \
  --region "${REGION}" \
  --service-account "${SA_EMAIL}" \
  --allow-unauthenticated \
  --min-instances 0 --max-instances 2 \
  --set-env-vars "GCP_PROJECT_ID=${PROJECT_ID},GCP_LOCATION=${DOCAI_LOCATION},GCS_BUCKET_NAME=${BUCKET}" \
  --set-secrets "TELEGRAM_BOT_TOKEN=TELEGRAM_BOT_TOKEN:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,DOCAI_PROCESSOR_ID=DOCAI_PROCESSOR_ID:latest,SCHEDULER_TOKEN=SCHEDULER_TOKEN:latest"

SERVICE_URL=$(gcloud run services describe "${SERVICE}" --region "${REGION}" --format='value(status.url)')
TOKEN_HASH=$(printf '%s' "${TELEGRAM_BOT_TOKEN}" | sha256sum | cut -c1-32)

echo "▸ Registering Telegram webhook…"
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=${SERVICE_URL}/webhook/${TOKEN_HASH}"
echo

echo "▸ Cloud Scheduler cron (daily 07:00 UTC)…"
gcloud scheduler jobs create http lifekeeper-cron \
  --location "${REGION}" \
  --schedule "0 7 * * *" \
  --uri "${SERVICE_URL}/tasks/run-scheduler" \
  --http-method POST \
  --headers "X-Scheduler-Token=${SCHEDULER_TOKEN}" || true

echo
echo "✅ Done. Service: ${SERVICE_URL}"
echo "   Demo trigger: curl -X POST ${SERVICE_URL}/tasks/run-scheduler -H \"X-Scheduler-Token: \$SCHEDULER_TOKEN\""
