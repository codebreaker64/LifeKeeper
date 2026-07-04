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
import os
from typing import Any
from dotenv import load_dotenv

from google.adk.apps import App
from google.adk.workflow import Workflow, node, START
from google.adk.events import RequestInput
from google.genai import Client
from google.genai import types
from pydantic import BaseModel, Field

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

# In-memory database to store verified documents
# Format: { user_id: { doc_hash: record } }
IN_MEMORY_STORE = {}

# In-memory store for pending confirmations (low confidence)
# Format: { user_id: { doc_hash: record } }
PENDING_CONFIRMATIONS = {}


class ExtractionResult(BaseModel):
    document_type: str = Field(description="The type of document, e.g., Passport, Driving Licence, Insurance, Warranty, Other.")
    renewal_deadline: str = Field(description="The renewal deadline or expiration date in YYYY-MM-DD format. If not found or not applicable, return None.")
    confidence_score: float = Field(description="A confidence score from 0.0 to 1.0 indicating how certain we are about the renewal deadline extraction.")
    justification: str = Field(description="A brief justification for the extracted deadline and confidence score.")


class IngestionArgs(BaseModel):
    user_id: str = Field(description="The user ID specified by the user. If not found, return empty string.")
    base64_file: str = Field(description="The base64-encoded document content. If not found, return empty string.")
    mime_type: str = Field(description="The MIME type of the file, e.g., 'application/pdf', 'image/jpeg', 'image/png'. Default to 'application/pdf' if not clear.")


class ConfirmArgs(BaseModel):
    user_id: str = Field(description="The user ID.")
    doc_hash: str = Field(description="The document hash.")


class ListArgs(BaseModel):
    user_id: str = Field(description="The user ID.")


def ingest_document(user_id: str, base64_file: str, mime_type: str) -> str:
    """Ingests a personal document, extracts its renewal deadline using Gemini, and stores it.

    Args:
        user_id: The ID of the user.
        base64_file: The base64-encoded document or image.
        mime_type: The MIME type of the file (e.g. 'application/pdf', 'image/jpeg', 'image/png').

    Returns:
        A message describing the status of the ingestion.
    """
    user_id_clean = user_id.strip()
    if not user_id_clean:
        return "Error: User ID cannot be empty."

    if not base64_file.strip():
        return "Error: Base64 file content cannot be empty."

    # Compute a unique hash to deduplicate documents per user
    doc_hash = hashlib.sha256(base64_file.encode("utf-8")).hexdigest()[:16]

    # Deduplicate: check if document already exists
    if user_id_clean in IN_MEMORY_STORE and doc_hash in IN_MEMORY_STORE[user_id_clean]:
        return f"Document already exists in user '{user_id_clean}' records (hash: {doc_hash})."

    if user_id_clean in PENDING_CONFIRMATIONS and doc_hash in PENDING_CONFIRMATIONS[user_id_clean]:
        return f"Document is already uploaded and pending confirmation for user '{user_id_clean}' (hash: {doc_hash})."

    # Decode base64 content
    try:
        file_bytes = base64.b64decode(base64_file)
    except Exception as e:
        return f"Error: Failed to decode base64 file content. Details: {e}"

    # Call Gemini model via official SDK to extract metadata
    try:
        client = Client()
        prompt = (
            "Analyze the attached document (which could be a passport, driving licence, "
            "insurance policy, or warranty) to determine the document type and its renewal "
            "or expiration deadline. Please provide a confidence score (from 0.0 to 1.0) "
            "for the extracted deadline, and write a brief justification."
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(
                    data=file_bytes,
                    mime_type=mime_type
                ),
                prompt
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ExtractionResult,
            )
        )

        extraction: ExtractionResult = response.parsed
    except Exception as e:
        return f"Error occurred during Gemini extraction: {e}"

    if not extraction:
        return "Error: Gemini failed to return a structured response."

    # Form the record
    record = {
        "doc_hash": doc_hash,
        "document_type": extraction.document_type,
        "renewal_deadline": extraction.renewal_deadline,
        "confidence_score": extraction.confidence_score,
        "justification": extraction.justification,
    }

    # Handle confidence checking
    if extraction.confidence_score < 0.8:
        if user_id_clean not in PENDING_CONFIRMATIONS:
            PENDING_CONFIRMATIONS[user_id_clean] = {}
        PENDING_CONFIRMATIONS[user_id_clean][doc_hash] = record

        return (
            f"Low confidence extraction ({extraction.confidence_score:.2f}). "
            f"Document Type: {extraction.document_type}, Deadline: {extraction.renewal_deadline}. "
            f"Justification: {extraction.justification}. "
            f"This record is PENDING. Please run the confirm_document tool "
            f"with hash '{doc_hash}' to persist this record."
        )
    else:
        if user_id_clean not in IN_MEMORY_STORE:
            IN_MEMORY_STORE[user_id_clean] = {}
        IN_MEMORY_STORE[user_id_clean][doc_hash] = record

        return (
            f"High confidence extraction ({extraction.confidence_score:.2f}). "
            f"Successfully persisted record. "
            f"Document Type: {extraction.document_type}, Deadline: {extraction.renewal_deadline}. "
            f"Justification: {extraction.justification}."
        )


def confirm_document(user_id: str, doc_hash: str) -> str:
    """Confirms and persists a pending low-confidence document extraction.

    Args:
        user_id: The ID of the user.
        doc_hash: The hash of the pending document.

    Returns:
        A message indicating the confirmation result.
    """
    user_id_clean = user_id.strip()
    doc_hash_clean = doc_hash.strip()

    if user_id_clean not in PENDING_CONFIRMATIONS or doc_hash_clean not in PENDING_CONFIRMATIONS[user_id_clean]:
        return f"Error: No pending document found with hash '{doc_hash_clean}' for user '{user_id_clean}'."

    # Move from pending to verified
    record = PENDING_CONFIRMATIONS[user_id_clean].pop(doc_hash_clean)
    if not PENDING_CONFIRMATIONS[user_id_clean]:
        del PENDING_CONFIRMATIONS[user_id_clean]

    if user_id_clean not in IN_MEMORY_STORE:
        IN_MEMORY_STORE[user_id_clean] = {}

    IN_MEMORY_STORE[user_id_clean][doc_hash_clean] = record
    return (
        f"Success: Confirmed and persisted document record for user '{user_id_clean}'. "
        f"Type: {record['document_type']}, Deadline: {record['renewal_deadline']}."
    )


def list_documents(user_id: str) -> str:
    """Lists all stored and pending document records for a given user.

    Args:
        user_id: The ID of the user.

    Returns:
        A string listing all stored and pending records.
    """
    user_id_clean = user_id.strip()
    result = []

    # Stored documents
    stored = IN_MEMORY_STORE.get(user_id_clean, {})
    if stored:
        result.append("Persisted Documents:")
        for doc_hash, record in stored.items():
            result.append(
                f"- Hash: {doc_hash} | Type: {record['document_type']} | "
                f"Deadline: {record['renewal_deadline']} | "
                f"Confidence: {record['confidence_score']:.2f}"
            )
    else:
        result.append("No persisted documents found.")

    # Pending documents
    pending = PENDING_CONFIRMATIONS.get(user_id_clean, {})
    if pending:
        result.append("\nPending Confirmation Documents:")
        for doc_hash, record in pending.items():
            result.append(
                f"- Hash: {doc_hash} | Type: {record['document_type']} | "
                f"Deadline: {record['renewal_deadline']} | "
                f"Confidence: {record['confidence_score']:.2f} | "
                f"Justification: {record['justification']}"
            )

    return "\n".join(result)


# --- Workflow Graph Nodes ---

@node
async def router_node(ctx, node_input: str) -> str:
    """Classifies user intent and routes to the appropriate node."""
    client = Client()
    prompt = (
        "Classify the user request into one of the following intents:\n"
        "- 'ingest': The user wants to upload, ingest, parse, or add a document. They might provide base64 data.\n"
        "- 'confirm': The user is explicitly confirming a pending document or a hash.\n"
        "- 'list': The user wants to view, list, or check their documents.\n"
        "- 'chat': None of the above (general queries, greetings, or questions).\n\n"
        f"User Request: {node_input}\n\n"
        "Return ONLY the intent string ('ingest', 'confirm', 'list', or 'chat') without any other text."
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    intent = response.text.strip().lower()
    if intent not in ("ingest", "confirm", "list", "chat"):
        intent = "chat"

    ctx.route = intent
    return node_input


@node(rerun_on_resume=True)
async def ingest_node(ctx, node_input: str) -> Any:
    """Handles document ingestion and requests human confirmation for low confidence extractions."""
    # Check if we are resuming from human confirmation
    if "pending_extraction" in ctx.state:
        reply = node_input.strip().lower()
        pending = ctx.state["pending_extraction"]
        user_id = pending["user_id"]
        doc_hash = pending["doc_hash"]

        # Clean up session state
        del ctx.state["pending_extraction"]

        if reply in ("yes", "confirm", "y", "sure"):
            yield confirm_document(user_id, doc_hash)
            return

        # Clean up pending store if rejected
        if user_id in PENDING_CONFIRMATIONS and doc_hash in PENDING_CONFIRMATIONS[user_id]:
            del PENDING_CONFIRMATIONS[user_id][doc_hash]
        yield "Ingestion discarded by user."
        return

    # Parse args from user request using LLM
    client = Client()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=f"Extract the user ID, base64 file content, and mime type from the text:\n\n{node_input}",
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=IngestionArgs
        )
    )
    args: IngestionArgs = response.parsed

    if not args.user_id:
        yield "Error: Missing user ID. Please provide your user ID to ingest the document."
        return
    if not args.base64_file:
        yield "Error: Missing base64 file content. Please provide the base64-encoded document."
        return

    res = ingest_document(args.user_id, args.base64_file, args.mime_type)

    # Check if the document was placed in the pending confirmations store
    doc_hash = hashlib.sha256(args.base64_file.encode("utf-8")).hexdigest()[:16]
    if args.user_id in PENDING_CONFIRMATIONS and doc_hash in PENDING_CONFIRMATIONS[args.user_id]:
        ctx.state["pending_extraction"] = {
            "user_id": args.user_id,
            "doc_hash": doc_hash
        }
        # Yield RequestInput to pause the workflow and ask for confirmation
        yield RequestInput(message=f"{res}\n\nWould you like to confirm and persist this record? (yes/no)")
        return

    yield res


@node
async def confirm_node(ctx, node_input: str) -> str:
    """Handles explicit confirmation of a pending document."""
    client = Client()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=f"Extract the user ID and document hash from the text:\n\n{node_input}",
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ConfirmArgs
        )
    )
    args: ConfirmArgs = response.parsed
    if not args.user_id or not args.doc_hash:
        return "Error: Could not extract user ID or document hash to confirm. Please provide them clearly."

    return confirm_document(args.user_id, args.doc_hash)


@node
async def list_node(ctx, node_input: str) -> str:
    """Lists all stored and pending documents for a user."""
    client = Client()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=f"Extract the user ID from the text:\n\n{node_input}",
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ListArgs
        )
    )
    args: ListArgs = response.parsed
    if not args.user_id:
        return "Error: Missing user ID. Please provide your user ID to list your documents."

    return list_documents(args.user_id)


@node
async def chat_node(ctx, node_input: str) -> str:
    """Provides general assistant replies."""
    client = Client()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=(
            "You are an AI life-admin renewal assistant. Help the user generally with their query. "
            "Remind them they can ingest documents (passports, driving licences, etc.) by providing base64 content "
            f"and a user ID.\n\nUser query: {node_input}"
        )
    )
    return response.text


# Build the Workflow Graph
workflow = Workflow(
    name="lifekeeper_workflow",
    edges=[
        (START, router_node),
        (router_node, {
            "ingest": ingest_node,
            "confirm": confirm_node,
            "list": list_node,
            "chat": chat_node
        })
    ]
)

root_agent = workflow

app = App(
    root_agent=root_agent,
    name="app",
)
