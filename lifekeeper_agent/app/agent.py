# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import hashlib
import json
import os
import re
from typing import Any
from dotenv import load_dotenv

from google.adk.apps import App
from google.adk.workflow import Workflow, node, START
from google.adk.events import RequestInput
from google.genai import Client
from google.genai import types
from pydantic import BaseModel, Field

# Import configurations
from app.config import CONFIDENCE_THRESHOLD, GEMINI_MODEL_NAME

# Load environment variables
load_dotenv()

# Configure environment for Gemini API Client
if os.environ.get("GEMINI_API_KEY"):
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
else:
    try:
        import google.auth
        _, project_id = google.auth.default()
        os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
        os.environ["GOOGLE_CLOUD_LOCATION"] = "us-central1"
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
    except Exception:
        pass

# In-memory database to store verified documents and security logs
# Format: { doc_hash: record }
IN_MEMORY_STORE = {}


class DocumentDetails(BaseModel):
    doc_type: str = Field(description="The type of document, e.g., Passport, Driving Licence, Insurance, Warranty.")
    owner_name: str = Field(description="The full name of the owner of the document.")
    expiry_date: str = Field(description="The expiration or renewal date of the document in YYYY-MM-DD format.")
    reference_number: str = Field(description="The document's reference number or serial ID.")
    confidence_score: float = Field(description="A confidence score between 0.0 and 1.0 indicating your certainty about the extracted details.")
    uncertainties: list[str] = Field(description="A list of specific details or fields that were unclear, blurry, missing, or uncertain.")


# --- Helper Functions for Security Checkpoint ---

async def ocr_document(file_bytes: bytes, mime_type: str) -> str:
    """Calls Gemini to perform verbatim text extraction / OCR on the document."""
    client = Client()
    response = client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=[
            types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
            "Extract and return all the raw text from the document verbatim. Do not interpret, reformat, or modify anything."
        ]
    )
    return response.text or ""


def detect_prompt_injection(text: str) -> bool:
    """Scans the document text for instructions attempting to hijack execution or bypass confidence gates."""
    normalized = text.lower()
    injection_patterns = [
        "ignore previous instructions",
        "ignore system instructions",
        "set confidence to 1.0",
        "set confidence",
        "bypass the confidence gate",
        "force an auto-persist",
        "override confidence",
        "bypass the gate"
    ]
    for pattern in injection_patterns:
        if pattern in normalized:
            return True
    return False


def scrub_pii(text: str) -> tuple[str, list[str]]:
    """Scrubs sensitive PII like SSNs and credit card numbers from the extracted text."""
    redacted = []
    scrubbed = text

    # SSN Pattern (9 digits or standard hyphens)
    ssn_pattern = r"\b\d{3}-\d{2}-\d{4}\b"
    if re.search(ssn_pattern, scrubbed):
        redacted.append("SSN")
        scrubbed = re.sub(ssn_pattern, "[REDACTED_SSN]", scrubbed)

    # Credit Card Pattern (13 to 16 digits)
    cc_pattern = r"\b(?:\d[ -]*?){13,16}\b"
    if re.search(cc_pattern, scrubbed):
        redacted.append("Credit Card")
        scrubbed = re.sub(cc_pattern, "[REDACTED_CC]", scrubbed)

    # Email addresses
    email_pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
    if re.search(email_pattern, scrubbed):
        redacted.append("Email")
        scrubbed = re.sub(email_pattern, "[REDACTED_EMAIL]", scrubbed)

    return scrubbed, redacted


# --- Workflow Graph Nodes ---

@node
async def security_checkpoint_node(ctx, node_input: Any) -> Any:
    """First-gate node to OCR the document, redact PII, and block prompt injections."""
    # Normalize input
    if isinstance(node_input, dict):
        data = node_input
    else:
        if isinstance(node_input, types.Content):
            text_input = "".join(part.text for part in node_input.parts if part.text is not None)
        elif isinstance(node_input, str):
            text_input = node_input
        else:
            text_input = str(node_input)

        try:
            data = json.loads(text_input)
        except Exception:
            yield "Error: Input must be a valid JSON string or dictionary containing 'base64_file' and 'mime_type'."
            return

    base64_file = data.get("base64_file", "")
    mime_type = data.get("mime_type", "application/pdf")

    if not base64_file:
        yield "Error: Missing 'base64_file' key in input."
        return

    doc_hash = hashlib.sha256(base64_file.encode("utf-8")).hexdigest()[:16]

    if doc_hash in IN_MEMORY_STORE:
        yield f"Document already exists in the renewal store (hash: {doc_hash})."
        return

    try:
        file_bytes = base64.b64decode(base64_file)
    except Exception as e:
        yield f"Error: Failed to decode base64 file content. Details: {e}"
        return

    # 1. OCR text extraction
    try:
        raw_text = await ocr_document(file_bytes, mime_type)
    except Exception as e:
        yield f"Error performing OCR extraction: {e}"
        return

    # 2. Defend against Prompt Injection
    if detect_prompt_injection(raw_text):
        ctx.state["security_event"] = {
            "doc_hash": doc_hash,
            "raw_text": raw_text
        }
        ctx.route = "security_flagged"
        yield f"[SECURITY WARNING] Potential prompt injection detected in document (hash: {doc_hash}). Routing to security review."
        return

    # 3. Scrub PII
    scrubbed_text, redacted_categories = scrub_pii(raw_text)

    # Cache clean details in state for downstream extract_node
    ctx.state["scrubbed_text"] = scrubbed_text
    ctx.state["redacted_categories"] = redacted_categories
    ctx.state["doc_hash"] = doc_hash

    ctx.route = "clean"
    yield "Document passed security checkpoint. Proceeding to extraction."


@node(rerun_on_resume=True)
async def security_review_node(ctx, node_input: Any) -> Any:
    """Manages manual human review for documents flagged with prompt injections."""
    if not ctx.state.get("awaiting_security_decision"):
        ctx.state["awaiting_security_decision"] = True
        event = ctx.state.get("security_event", {})
        doc_hash = event.get("doc_hash", "unknown")
        yield RequestInput(
            message=(
                f"[MANUAL SECURITY REVIEW REQUIRED]\n\n"
                f"A prompt injection attempt was detected in document (hash: {doc_hash}).\n\n"
                "Would you like to confirm and FORCE PERSIST this document as a security exception, or DISCARD it? (confirm/discard)"
            )
        )
        return

    # Parse resume decision
    if isinstance(node_input, types.Content):
        decision_str = "".join(part.text for part in node_input.parts if part.text is not None)
    else:
        decision_str = str(node_input)
    decision = decision_str.strip().lower()

    del ctx.state["awaiting_security_decision"]
    event = ctx.state.pop("security_event", None)
    doc_hash = event.get("doc_hash", "unknown") if event else "unknown"

    if decision in ("confirm", "force", "yes", "y"):
        IN_MEMORY_STORE[doc_hash] = {
            "security_event": True,
            "status": "FORCE_PERSISTED",
            "raw_text": event.get("raw_text", "") if event else ""
        }
        yield f"Security Action: Document (hash: {doc_hash}) has been FORCE PERSISTED by administrator."
    else:
        IN_MEMORY_STORE[doc_hash] = {
            "security_event": True,
            "status": "DISCARDED"
        }
        yield f"Security Action: Document (hash: {doc_hash}) has been DISCARDED."


@node
async def extract_node(ctx, node_input: Any) -> Any:
    """Extracts document details from scrubbed text and applies confidence routing rules."""
    scrubbed_text = ctx.state.get("scrubbed_text", "")
    redacted_categories = ctx.state.get("redacted_categories", [])
    doc_hash = ctx.state.get("doc_hash", "")

    if not scrubbed_text:
        yield "Error: No scrubbed text found in session state."
        return

    client = Client()
    try:
        prompt = (
            "Analyze the following scrubbed document text to extract the document type, owner's full name, "
            "expiry/renewal date, and reference/serial number. "
            "Also assess your confidence score (0.0 to 1.0) and identify any specific fields "
            "or details you are uncertain about.\n\n"
            f"Scrubbed Document Text:\n{scrubbed_text}"
        )
        response = client.models.generate_content(
            model=GEMINI_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=DocumentDetails
            )
        )
        details: DocumentDetails = response.parsed
    except Exception as e:
        yield f"Error occurred during Gemini extraction: {e}"
        return

    if not details:
        yield "Error: Gemini failed to return a structured response."
        return

    # Confidence Threshold Checking
    if details.confidence_score >= CONFIDENCE_THRESHOLD:
        # Auto-persist directly to the in-memory store
        record = details.model_dump()
        record["redacted_categories"] = redacted_categories
        IN_MEMORY_STORE[doc_hash] = record
        ctx.route = "auto_persist"
        yield f"Successfully auto-persisted record for {details.owner_name} ({details.doc_type}). Confidence: {details.confidence_score:.2f}."
    else:
        # Low confidence: use LLM to summarize findings and uncertainties without leakages
        summary_prompt = (
            f"Generate a user-friendly summary of the extracted document details and clearly flag the uncertainties. "
            f"Crucial rule: Your summary MUST NOT contain any PII (SSNs, credit card numbers, etc.).\n\n"
            f"Extracted Details: {details.model_dump()}\n\n"
            "Write a short, clear summary (2-3 sentences) detailing what you found and what was uncertain."
        )
        try:
            summary_response = client.models.generate_content(
                model=GEMINI_MODEL_NAME,
                contents=summary_prompt
            )
            summary_text = summary_response.text.strip()
        except Exception as e:
            summary_text = f"Extracted {details.doc_type} with low confidence. Uncertainties: {', '.join(details.uncertainties)}."

        # Save pending extraction to session state
        ctx.state["pending_document"] = {
            "doc_hash": doc_hash,
            "details": details.model_dump(),
            "summary": summary_text,
            "redacted_categories": redacted_categories
        }
        ctx.route = "requires_confirmation"
        yield f"Extraction confidence is low ({details.confidence_score:.2f}). Forwarding for review."


@node(rerun_on_resume=True)
async def confirmation_node(ctx, node_input: Any) -> Any:
    """Pauses the workflow to request human confirmation for low confidence extractions."""
    if not ctx.state.get("awaiting_decision"):
        ctx.state["awaiting_decision"] = True
        pending = ctx.state.get("pending_document", {})
        summary = pending.get("summary", "No summary available.")

        yield RequestInput(
            message=(
                f"Verification Required:\n\n"
                f"{summary}\n\n"
                "Would you like to confirm and persist this record? (yes/no)"
            )
        )
        return

    # Process response
    if isinstance(node_input, types.Content):
        decision_str = "".join(part.text for part in node_input.parts if part.text is not None)
    else:
        decision_str = str(node_input)
    decision = decision_str.strip().lower()

    # Clean up state flags
    del ctx.state["awaiting_decision"]
    pending = ctx.state.pop("pending_document", None)

    if not pending:
        yield "Error: No pending document was found in session state."
        return

    doc_hash = pending["doc_hash"]
    details = pending["details"]
    redacted_categories = pending.get("redacted_categories", [])

    if decision in ("yes", "confirm", "y", "sure"):
        # Persist the record including redacted categories
        details["redacted_categories"] = redacted_categories
        IN_MEMORY_STORE[doc_hash] = details
        yield f"Success: Stored confirmed document (hash: {doc_hash}, owner: {details['owner_name']})."
    else:
        yield f"Discarded: Document extraction discarded for hash {doc_hash}."


# Build the Workflow Graph
workflow = Workflow(
    name="lifekeeper_workflow",
    edges=[
        (START, security_checkpoint_node),
        (security_checkpoint_node, {
            "security_flagged": security_review_node,
            "clean": extract_node
        }),
        (extract_node, {
            "requires_confirmation": confirmation_node
        })
    ]
)

root_agent = workflow

app = App(
    root_agent=root_agent,
    name="app",
)
