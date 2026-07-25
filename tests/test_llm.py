"""Verify the Anthropic requests we build are actually valid on the wire.

The SDK is pointed at a local mock endpoint so each request is fully
constructed, serialized, and sent. This catches wrong parameter names or
shapes that a pure `unittest.mock` stub would happily accept.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

STREAM_EVENTS = [
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": "# Tailored Resume"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta",
                       "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                       "usage": {"output_tokens": 5}}),
    ("message_stop", {"type": "message_stop"}),
]

REVIEW_PAYLOAD = {
    "score": 78, "verdict": "worth applying", "strengths": ["Billing systems"],
    "gaps": ["No Kafka"], "talking_points": ["Lead with ledger work"],
    "resume_advice": "Put payments first.",
}

PROFILE = {"name": "Alex", "skills": ["Python"],
           "experience": [{"company": "Acme", "title": "Engineer", "bullets": ["Built the ledger"]}]}
JOB = {"company": "Northstar", "title": "Senior Backend Engineer", "location": "Austin",
       "remote": False, "url": "https://example.com/job", "description": "Python and Go."}


class _MockAnthropic:
    """Minimal stand-in for POST /v1/messages, streaming and non-streaming."""

    def __init__(self):
        self.requests: list[dict] = []
        self.stop_reason = "end_turn"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence the default stderr logging
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for event, data in STREAM_EVENTS:
                        self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
                        self.wfile.flush()
                    return
                message = {
                    "id": "msg_2", "type": "message", "role": "assistant",
                    "model": "claude-opus-5", "stop_sequence": None,
                    "content": ([] if outer.stop_reason == "refusal"
                                else [{"type": "text", "text": json.dumps(REVIEW_PAYLOAD)}]),
                    "stop_reason": outer.stop_reason,
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                }
                payload = json.dumps(message).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture()
def mock_anthropic(monkeypatch):
    mock = _MockAnthropic()
    from app import llm

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{mock.port}")
    monkeypatch.setattr(llm.config, "ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(llm, "_client", None)
    yield mock
    monkeypatch.setattr(llm, "_client", None)
    mock.server.shutdown()


def test_streaming_request_shape(mock_anthropic):
    from app import tailor

    text = tailor.tailored_resume(PROFILE, JOB)
    assert text == "# Tailored Resume"

    request = mock_anthropic.requests[-1]
    assert request["model"] == "claude-opus-5"
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["effort"] == "high"
    assert request["stream"] is True
    # The anti-fabrication rule must reach the model on every resume call.
    assert "ONLY facts present" in request["system"]
    assert "Northstar" in request["messages"][0]["content"]


def test_structured_output_request_and_parse(mock_anthropic):
    from app import tailor

    review = tailor.deep_review(PROFILE, JOB)
    assert review["score"] == 78 and review["verdict"] == "worth applying"

    output_config = mock_anthropic.requests[-1]["output_config"]
    assert output_config["format"]["type"] == "json_schema"
    assert output_config["format"]["schema"]["additionalProperties"] is False
    assert "score" in output_config["format"]["schema"]["required"]


def test_interview_prep_uses_generous_token_budget(mock_anthropic):
    from app import prep

    assert prep.interview_prep(PROFILE, JOB)
    assert mock_anthropic.requests[-1]["max_tokens"] >= 8000


def test_refusal_becomes_llm_error(mock_anthropic):
    from app import llm, tailor

    mock_anthropic.stop_reason = "refusal"
    with pytest.raises(llm.LLMError, match="declined"):
        tailor.deep_review(PROFILE, JOB)


def test_missing_credentials_raise_actionable_error(monkeypatch):
    from app import llm

    monkeypatch.setattr(llm, "available", lambda: False)
    monkeypatch.setattr(llm, "_client", None)
    with pytest.raises(llm.LLMError, match="ANTHROPIC_API_KEY"):
        llm.complete("system", "prompt")
