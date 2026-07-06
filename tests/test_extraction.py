"""Extraction Agent validation — the Gemini client is faked, so these test
the Python re-validation layer ('never trust the model')."""

import json
from datetime import timezone
from types import SimpleNamespace

import pytest

from app.agents.extraction import ExtractionError, extract_record


def _fake_genai(monkeypatch, response_text: str) -> None:
    """Replace google.genai.Client with a stub whose generate_content
    returns `response_text` verbatim."""

    class FakeModels:
        def generate_content(self, **kwargs):
            return SimpleNamespace(text=response_text)

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = FakeModels()

    monkeypatch.setattr("google.genai.Client", FakeClient)


def test_valid_extraction(monkeypatch):
    _fake_genai(monkeypatch, json.dumps({
        "doc_type": "passport",
        "expiry_date": "2030-05-01",
        "owner_name": "Ada Lovelace",
        "reference_number": "P-123",
    }))
    record = extract_record("some ocr text")
    assert record["doc_type"] == "passport"
    assert record["expiry_date"].tzinfo == timezone.utc
    assert record["expiry_date"].year == 2030
    assert record["owner_name"] == "Ada Lovelace"


def test_unknown_doc_type_falls_back_to_other(monkeypatch):
    _fake_genai(monkeypatch, json.dumps({
        "doc_type": "spaceship", "expiry_date": "2030-05-01",
    }))
    assert extract_record("text")["doc_type"] == "other"


def test_missing_expiry_raises_extraction_error(monkeypatch):
    _fake_genai(monkeypatch, json.dumps({"doc_type": "passport"}))
    with pytest.raises(ExtractionError):
        extract_record("text")


def test_unparseable_date_raises_extraction_error(monkeypatch):
    _fake_genai(monkeypatch, json.dumps({
        "doc_type": "passport", "expiry_date": "not-a-date",
    }))
    with pytest.raises(ExtractionError):
        extract_record("text")


def test_issuing_country_name_is_normalised(monkeypatch):
    _fake_genai(monkeypatch, json.dumps({
        "doc_type": "passport", "expiry_date": "2030-05-01",
        "issuing_country": "Singapore",
    }))
    assert extract_record("text")["issuing_country"] == "SG"


def test_issuing_country_iso_code_passes_through(monkeypatch):
    _fake_genai(monkeypatch, json.dumps({
        "doc_type": "passport", "expiry_date": "2030-05-01",
        "issuing_country": "gb",
    }))
    assert extract_record("text")["issuing_country"] == "GB"


def test_missing_issuing_country_is_none(monkeypatch):
    _fake_genai(monkeypatch, json.dumps({
        "doc_type": "passport", "expiry_date": "2030-05-01",
    }))
    assert extract_record("text")["issuing_country"] is None


def test_model_json_glitch_is_not_extraction_error(monkeypatch):
    """A malformed Gemini response must surface as a JSON error (generic
    failure to the user), NOT as 'no expiry date found'."""
    _fake_genai(monkeypatch, "definitely not json")
    with pytest.raises(json.JSONDecodeError):
        extract_record("text")
