"""The Gemini backend: provider selection, request shape, and parsing.

No network — the SDK client is replaced with a fake that records the call and
returns a canned response, so the request we build and the response we parse are
both exercised for real.
"""

import json

import pytest

from app import llm

pytest.importorskip("google.genai", reason="google-genai is an optional dependency")

PROFILE = {"name": "Alex", "skills": ["Python"],
           "experience": [{"company": "Acme", "title": "Engineer", "bullets": ["Built the ledger"]}]}
JOB = {"company": "Northstar", "title": "Senior Backend Engineer", "location": "Karachi",
       "remote": False, "url": "https://example.com/job", "description": "Python and Go."}

REVIEW_PAYLOAD = {
    "score": 72, "verdict": "worth applying", "strengths": ["Python"],
    "gaps": ["No Go"], "talking_points": ["Lead with ledger work"],
    "resume_advice": "Put backend first.",
}


class _FakeResponse:
    def __init__(self, text):
        self.text = text
        self.prompt_feedback = None


class _FakeModels:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self.reply)


class _FakeClient:
    def __init__(self, reply):
        self.models = _FakeModels(reply)


@pytest.fixture()
def gemini(monkeypatch):
    """Force the Gemini provider and hand it a fake client with a fixed reply."""
    monkeypatch.setattr(llm.config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "gemini")

    def use(reply):
        client = _FakeClient(reply)
        monkeypatch.setattr(llm, "_gemini_client", client)
        # `_get_gemini_client` returns the cached client, so the fake stands in.
        monkeypatch.setattr(llm, "_get_gemini_client", lambda: client)
        return client

    return use


# --- provider selection -------------------------------------------------------


def test_gemini_key_alone_selects_gemini(monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "")
    monkeypatch.setattr(llm.config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(llm, "_anthropic_available", lambda: False)
    monkeypatch.setattr(llm.config, "GEMINI_API_KEY", "test-key")
    assert llm.provider() == "gemini"
    assert llm.available() is True
    assert llm.model_name() == llm.config.GEMINI_MODEL


def test_anthropic_wins_when_both_are_present(monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "")
    monkeypatch.setattr(llm, "_anthropic_available", lambda: True)
    monkeypatch.setattr(llm.config, "GEMINI_API_KEY", "test-key")
    assert llm.provider() == "anthropic"


def test_explicit_override_forces_gemini(monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(llm, "_anthropic_available", lambda: True)
    monkeypatch.setattr(llm.config, "GEMINI_API_KEY", "test-key")
    assert llm.provider() == "gemini"


def test_status_reports_both_backends(monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(llm.config, "GEMINI_API_KEY", "test-key")
    status = llm.status()
    assert status["provider"] == "gemini"
    assert status["available"] is True
    assert "gemini_ready" in status and "anthropic_ready" in status


# --- completions --------------------------------------------------------------


def test_complete_sends_system_and_token_budget(gemini):
    from app import tailor

    client = gemini("# Tailored Resume")
    text = tailor.tailored_resume(PROFILE, JOB)
    assert text == "# Tailored Resume"

    call = client.models.calls[-1]
    assert call["model"] == llm.config.GEMINI_MODEL
    assert "ONLY facts present" in call["config"].system_instruction
    assert "Northstar" in call["contents"]
    assert call["config"].max_output_tokens >= 4000


def test_complete_json_requests_json_and_parses(gemini):
    from app import tailor

    client = gemini(json.dumps(REVIEW_PAYLOAD))
    review = tailor.deep_review(PROFILE, JOB)
    assert review["score"] == 72 and review["verdict"] == "worth applying"

    call = client.models.calls[-1]
    assert call["config"].response_mime_type == "application/json"
    # The schema travels in the prompt, since Gemini won't take our full schema.
    assert "score" in call["contents"] and "verdict" in call["contents"]


def test_json_fences_are_stripped(gemini):
    from app import tailor

    gemini("```json\n" + json.dumps(REVIEW_PAYLOAD) + "\n```")
    review = tailor.deep_review(PROFILE, JOB)
    assert review["score"] == 72


def test_unparseable_json_is_a_clear_error(gemini):
    from app import tailor

    gemini("not json at all")
    with pytest.raises(llm.LLMError, match="unparseable"):
        tailor.deep_review(PROFILE, JOB)


def test_empty_response_is_a_clear_error(gemini):
    from app import tailor

    gemini("")
    with pytest.raises(llm.LLMError, match="empty"):
        tailor.tailored_resume(PROFILE, JOB)


def test_a_blocked_prompt_explains_itself(monkeypatch):
    monkeypatch.setattr(llm.config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "gemini")

    class _Blocked:
        text = None
        prompt_feedback = type("F", (), {"block_reason": "SAFETY"})()

    class _Models:
        def generate_content(self, **kwargs):
            return _Blocked()

    client = type("C", (), {"models": _Models()})()
    monkeypatch.setattr(llm, "_get_gemini_client", lambda: client)

    with pytest.raises(llm.LLMError, match="blocked"):
        llm.complete("system", "prompt")
