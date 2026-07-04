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
import pytest
from unittest.mock import MagicMock, patch

from app.agent import (
    ingest_document,
    confirm_document,
    list_documents,
    IN_MEMORY_STORE,
    PENDING_CONFIRMATIONS,
    ExtractionResult,
)


@pytest.fixture(autouse=True)
def reset_stores():
    """Resets the global stores before each test run."""
    IN_MEMORY_STORE.clear()
    PENDING_CONFIRMATIONS.clear()


def test_ingest_empty_user_id():
    """Verify that an empty user ID is rejected."""
    result = ingest_document(user_id="  ", base64_file="abc", mime_type="application/pdf")
    assert "Error: User ID cannot be empty" in result


def test_ingest_empty_base64_file():
    """Verify that empty base64 content is rejected."""
    result = ingest_document(user_id="user_1", base64_file="  ", mime_type="application/pdf")
    assert "Error: Base64 file content cannot be empty" in result


def test_ingest_invalid_base64():
    """Verify that invalid base64 content fails gracefully."""
    result = ingest_document(user_id="user_1", base64_file="invalid-base64!!!", mime_type="application/pdf")
    assert "Error: Failed to decode base64" in result


@patch("app.agent.Client")
def test_ingest_high_confidence_flow(mock_client_class):
    """Verify high-confidence document is automatically persisted."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.parsed = ExtractionResult(
        document_type="Passport",
        renewal_deadline="2030-05-15",
        confidence_score=0.9,
        justification="Highly legible passport expiry date.",
    )
    mock_client.models.generate_content.return_value = mock_response
    mock_client_class.return_value = mock_client

    dummy_b64 = base64.b64encode(b"passport data").decode("utf-8")
    result = ingest_document(user_id="user_1", base64_file=dummy_b64, mime_type="application/pdf")

    assert "Successfully persisted record" in result
    assert "Passport" in result
    assert "user_1" in IN_MEMORY_STORE
    doc_hash = list(IN_MEMORY_STORE["user_1"].keys())[0]
    assert IN_MEMORY_STORE["user_1"][doc_hash]["renewal_deadline"] == "2030-05-15"


@patch("app.agent.Client")
def test_ingest_low_confidence_flow(mock_client_class):
    """Verify low-confidence document is held as PENDING and requires confirmation."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.parsed = ExtractionResult(
        document_type="Insurance",
        renewal_deadline="2026-12-31",
        confidence_score=0.5,
        justification="Expiry date blurry.",
    )
    mock_client.models.generate_content.return_value = mock_response
    mock_client_class.return_value = mock_client

    dummy_b64 = base64.b64encode(b"insurance data").decode("utf-8")
    result = ingest_document(user_id="user_2", base64_file=dummy_b64, mime_type="image/png")

    assert "Low confidence extraction" in result
    assert "PENDING" in result
    assert "user_2" in PENDING_CONFIRMATIONS
    assert "user_2" not in IN_MEMORY_STORE

    doc_hash = list(PENDING_CONFIRMATIONS["user_2"].keys())[0]

    # Try to confirm with invalid details
    confirm_err = confirm_document(user_id="user_2", doc_hash="invalid_hash")
    assert "Error: No pending document found" in confirm_err

    # Confirm with correct details
    confirm_ok = confirm_document(user_id="user_2", doc_hash=doc_hash)
    assert "Success" in confirm_ok
    assert "user_2" in IN_MEMORY_STORE
    assert doc_hash in IN_MEMORY_STORE["user_2"]
    assert "user_2" not in PENDING_CONFIRMATIONS


@patch("app.agent.Client")
def test_ingest_deduplication_persisted(mock_client_class):
    """Verify duplicate document is blocked if already persisted."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.parsed = ExtractionResult(
        document_type="Passport",
        renewal_deadline="2030-05-15",
        confidence_score=0.9,
        justification="Clear.",
    )
    mock_client.models.generate_content.return_value = mock_response
    mock_client_class.return_value = mock_client

    dummy_b64 = base64.b64encode(b"duplicate test data").decode("utf-8")

    # Ingest once
    result1 = ingest_document(user_id="user_3", base64_file=dummy_b64, mime_type="application/pdf")
    assert "Successfully persisted" in result1

    # Ingest again (same user, same file)
    result2 = ingest_document(user_id="user_3", base64_file=dummy_b64, mime_type="application/pdf")
    assert "already exists" in result2
    assert len(IN_MEMORY_STORE["user_3"]) == 1


@patch("app.agent.Client")
def test_ingest_deduplication_pending(mock_client_class):
    """Verify duplicate document is blocked if already pending."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.parsed = ExtractionResult(
        document_type="Driving Licence",
        renewal_deadline="2027-01-01",
        confidence_score=0.4,
        justification="Too dark.",
    )
    mock_client.models.generate_content.return_value = mock_response
    mock_client_class.return_value = mock_client

    dummy_b64 = base64.b64encode(b"duplicate pending data").decode("utf-8")

    result1 = ingest_document(user_id="user_4", base64_file=dummy_b64, mime_type="image/jpeg")
    assert "PENDING" in result1

    result2 = ingest_document(user_id="user_4", base64_file=dummy_b64, mime_type="image/jpeg")
    assert "already uploaded and pending" in result2
    assert len(PENDING_CONFIRMATIONS["user_4"]) == 1


def test_list_documents_empty():
    """Verify listing returns clean message when no documents exist."""
    result = list_documents(user_id="empty_user")
    assert "No persisted documents found" in result


@patch("app.agent.Client")
def test_list_documents_with_records(mock_client_class):
    """Verify listing displays both persisted and pending records."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.parsed = ExtractionResult(
        document_type="Warranty",
        renewal_deadline="2029-01-01",
        confidence_score=0.85,
        justification="Legible date.",
    )
    mock_client.models.generate_content.return_value = mock_response
    mock_client_class.return_value = mock_client

    dummy_b64 = base64.b64encode(b"warranty raw data").decode("utf-8")
    ingest_document(user_id="user_5", base64_file=dummy_b64, mime_type="application/pdf")

    # Manually insert a pending document
    PENDING_CONFIRMATIONS["user_5"] = {
        "pending_hash_abc": {
            "doc_hash": "pending_hash_abc",
            "document_type": "Insurance",
            "renewal_deadline": "2026-06-01",
            "confidence_score": 0.5,
            "justification": "Blurry."
        }
    }

    list_output = list_documents(user_id="user_5")
    assert "Persisted Documents:" in list_output
    assert "Warranty" in list_output
    assert "Pending Confirmation Documents:" in list_output
    assert "Insurance" in list_output
