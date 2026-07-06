from google.cloud import documentai
from google.api_core.client_options import ClientOptions

PROJECT_ID = "lifekeeper0726"      # the *ID* (e.g. lifekeeper-2026), not the display name
LOCATION = "us"                        # must match the region you created the processor in
PROCESSOR_ID = "5e1adb0b5d0aefb7"      # just the hex ID, NOT projects/.../processors/...

client = documentai.DocumentProcessorServiceClient(
    client_options=ClientOptions(api_endpoint=f"{LOCATION}-documentai.googleapis.com")
)
name = client.processor_path(PROJECT_ID, LOCATION, PROCESSOR_ID)
print("Calling:", name)   # sanity-check the path with your own eyes

with open("test_passport.jpg", "rb") as f:
    img = f.read()

doc = client.process_document(
    request=documentai.ProcessRequest(
        name=name,
        raw_document=documentai.RawDocument(content=img, mime_type="image/jpeg"),
    )
).document
print(doc.text[:500])
