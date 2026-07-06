"""docai_test.py — one-off smoke test for the Document AI processor.

Usage:
    export GCP_PROJECT_ID=<project id>          # the ID, not the display name
    export DOCAI_PROCESSOR_ID=<hex id>          # NOT projects/.../processors/...
    python scripts/docai_test.py <image.jpg>
"""

import os
import sys

from google.api_core.client_options import ClientOptions
from google.cloud import documentai

PROJECT_ID = os.environ["GCP_PROJECT_ID"]
LOCATION = os.environ.get("GCP_LOCATION", "us")  # must match the processor's region
PROCESSOR_ID = os.environ["DOCAI_PROCESSOR_ID"]

image_path = sys.argv[1] if len(sys.argv) > 1 else "test_passport.jpg"

client = documentai.DocumentProcessorServiceClient(
    client_options=ClientOptions(api_endpoint=f"{LOCATION}-documentai.googleapis.com")
)
name = client.processor_path(PROJECT_ID, LOCATION, PROCESSOR_ID)
print("Calling:", name)   # sanity-check the path with your own eyes

with open(image_path, "rb") as f:
    img = f.read()

doc = client.process_document(
    request=documentai.ProcessRequest(
        name=name,
        raw_document=documentai.RawDocument(content=img, mime_type="image/jpeg"),
    )
).document
print(doc.text[:500])
