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


@patch("app.agent.Client")
def test_ingest_high_confidence(mock_client_class):
    """Verifies that high-confidence extractions are automatically persisted."""
    # Set up mock Gemini response
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.parsed = ExtractionResult(
        document_type="Passport",
        renewal_deadline="2030-05-15",
        confidence_score=0.9,
        justification="The passport expiry date is clearly visible on the main page.",
    )
    mock_client.models.generate_content.return_value = mock_response
    mock_client_class.return_value = mock_client

    # Dummy base64 encoded document
    dummy_b64 = base64.b64encode(b"dummy passport data").decode("utf-8")

    result = ingest_document(user_id="user_1", base64_file=dummy_b64, mime_type="application/pdf")

    assert "Successfully persisted record" in result
    assert "Passport" in result
    assert "2030-05-15" in result

    # Check that it is stored in IN_MEMORY_STORE
    assert "user_1" in IN_MEMORY_STORE
    docs = IN_MEMORY_STORE["user_1"]
    assert len(docs) == 1
    doc_hash = list(docs.keys())[0]
    assert docs[doc_hash]["document_type"] == "Passport"
    assert docs[doc_hash]["renewal_deadline"] == "2030-05-15"


@patch("app.agent.Client")
def test_ingest_low_confidence(mock_client_class):
    """Verifies that low-confidence extractions are stored as PENDING and require confirmation."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.parsed = ExtractionResult(
        document_type="Insurance Policy",
        renewal_deadline="2027-01-01",
        confidence_score=0.6,
        justification="The date is blurry and could not be clearly read.",
    )
    mock_client.models.generate_content.return_value = mock_response
    mock_client_class.return_value = mock_client

    dummy_b64 = base64.b64encode(b"dummy blurry insurance data").decode("utf-8")

    result = ingest_document(user_id="user_2", base64_file=dummy_b64, mime_type="image/jpeg")

    assert "Low confidence extraction" in result
    assert "PENDING" in result
    assert "2027-01-01" in result

    # Verify not in IN_MEMORY_STORE
    assert "user_2" not in IN_MEMORY_STORE

    # Verify in PENDING_CONFIRMATIONS
    assert "user_2" in PENDING_CONFIRMATIONS
    pending_docs = PENDING_CONFIRMATIONS["user_2"]
    assert len(pending_docs) == 1
    doc_hash = list(pending_docs.keys())[0]
    assert pending_docs[doc_hash]["document_type"] == "Insurance Policy"

    # Confirm the document and verify migration
    confirm_result = confirm_document(user_id="user_2", doc_hash=doc_hash)
    assert "Success" in confirm_result

    # Should now be in IN_MEMORY_STORE and removed from PENDING
    assert "user_2" in IN_MEMORY_STORE
    assert doc_hash in IN_MEMORY_STORE["user_2"]
    assert "user_2" not in PENDING_CONFIRMATIONS


@patch("app.agent.Client")
def test_ingest_deduplication(mock_client_class):
    """Verifies that duplicate document submissions are rejected per user."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.parsed = ExtractionResult(
        document_type="Driving Licence",
        renewal_deadline="2035-10-10",
        confidence_score=0.95,
        justification="Clear extraction.",
    )
    mock_client.models.generate_content.return_value = mock_response
    mock_client_class.return_value = mock_client

    dummy_b64 = base64.b64encode(b"driving licence unique content").decode("utf-8")

    # Ingest once
    result1 = ingest_document(user_id="user_3", base64_file=dummy_b64, mime_type="image/png")
    assert "Successfully persisted" in result1

    # Ingest again (duplicate check)
    result2 = ingest_document(user_id="user_3", base64_file=dummy_b64, mime_type="image/png")
    assert "already exists" in result2
    assert len(IN_MEMORY_STORE["user_3"]) == 1


@patch("app.agent.Client")
def test_list_documents(mock_client_class):
    """Verifies that list_documents accurately reports persisted and pending entries."""
    mock_client = MagicMock()
    mock_response1 = MagicMock()
    mock_response1.parsed = ExtractionResult(
        document_type="Warranty",
        renewal_deadline="2028-12-31",
        confidence_score=0.9,
        justification="Clean date.",
    )
    mock_client.models.generate_content.side_effect = [mock_response1]
    mock_client_class.return_value = mock_client

    dummy_b64_1 = base64.b64encode(b"warranty doc").decode("utf-8")

    # Ingest a high-confidence doc (persisted)
    ingest_document(user_id="user_4", base64_file=dummy_b64_1, mime_type="application/pdf")

    # Manually seed a pending document to avoid side_effect indexing complexity
    doc_hash_pending = "pendinghash123"
    PENDING_CONFIRMATIONS["user_4"] = {
        doc_hash_pending: {
            "doc_hash": doc_hash_pending,
            "document_type": "Passport",
            "renewal_deadline": "2032-01-01",
            "confidence_score": 0.5,
            "justification": "Very low confidence.",
        }
    }

    # List documents
    list_output = list_documents(user_id="user_4")
    assert "Persisted Documents:" in list_output
    assert "Warranty" in list_output
    assert "2028-12-31" in list_output
    assert "Pending Confirmation Documents:" in list_output
    assert "Passport" in list_output
    assert "2032-01-01" in list_output
