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
import json
from unittest.mock import MagicMock, patch
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import root_agent, DocumentDetails


@patch("app.agent.Client")
def test_agent_stream(mock_client_class) -> None:
    """
    Integration test for the agent stream functionality.
    Tests that the agent returns valid streaming responses.
    """
    # Mock Gemini Client and response
    mock_client = MagicMock()

    ocr_response = MagicMock()
    ocr_response.text = "Name: Alice Smith. Expiry: 2031-10-10. Ref: PS123456"

    extract_response = MagicMock()
    extract_response.parsed = DocumentDetails(
        doc_type="Passport",
        owner_name="Alice Smith",
        expiry_date="2031-10-10",
        reference_number="PS123456",
        confidence_score=0.95,
        uncertainties=[]
    )

    mock_client.models.generate_content.side_effect = [ocr_response, extract_response]
    mock_client_class.return_value = mock_client

    session_service = InMemorySessionService()

    session = session_service.create_session_sync(user_id="test_user", app_name="test")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="test")

    input_data = {
        "base64_file": base64.b64encode(b"passport content").decode("utf-8"),
        "mime_type": "application/pdf"
    }

    message = types.Content(
        role="user", parts=[types.Part.from_text(text=json.dumps(input_data))]
    )

    events = list(
        runner.run(
            new_message=message,
            user_id="test_user",
            session_id=session.id,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        )
    )
    assert len(events) > 0, "Expected at least one message"

    has_text_content = False
    for event in events:
        if event.output or (
            event.content
            and event.content.parts
            and any(part.text for part in event.content.parts)
        ):
            has_text_content = True
            break
    assert has_text_content, "Expected at least one message with text content"
