# cleanup.py — delete all documents + renewal_actions (keep users).
# Usage: GCP_PROJECT_ID=<project id> python scripts/cleanup.py
import os

from google.cloud import firestore

db = firestore.Client(project=os.environ.get("GCP_PROJECT_ID") or None)

for coll in ["documents", "renewal_actions"]:
    docs = list(db.collection(coll).stream())
    for d in docs:
        d.reference.delete()
    print(f"deleted {len(docs)} from {coll}")
