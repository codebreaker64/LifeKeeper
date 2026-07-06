# cleanup.py — delete all documents + renewal_actions (keep users)
from google.cloud import firestore

db = firestore.Client(project="lifekeeper0726")

for coll in ["documents", "renewal_actions"]:
    docs = list(db.collection(coll).stream())
    for d in docs:
        d.reference.delete()
    print(f"deleted {len(docs)} from {coll}")
