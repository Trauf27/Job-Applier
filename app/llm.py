"""LLM access, provider-agnostic.

The app was built on Anthropic's Claude, but not everyone has a key — so it also
speaks to Google's Gemini, which has a genuinely free tier. `tailor.py` and
`prep.py` call `complete()` and `complete_json()` and never learn which backend
answered; this module picks one and translates.

Provider selection (see `provider()`):
  * `LLM_PROVIDER=anthropic|gemini` forces a choice, else
  * Anthropic if its key or CLI login is present, else
  * Gemini if `GEMINI_API_KEY` is set.

Every feature degrades gracefully: with no usable provider, `available()` is
False and callers surface a clear message instead of raising.
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import config

try:  # Both SDKs are optional at import time so the app still boots without them.
    import anthropic
except ImportError:  # pragma: no cover - exercised only in stripped installs
    anthropic = None  # type: ignore[assignment]

try:
    from google import genai as google_genai
    from google.genai import types as google_genai_types
except ImportError:  # pragma: no cover
    google_genai = None  # type: ignore[assignment]
    google_genai_types = None  # type: ignore[assignment]

_client = None          # Anthropic client, lazily built
_gemini_client = None   # Gemini client, lazily built


class LLMError(RuntimeError):
    """Raised when a model call cannot be made or completed."""


# --- provider selection -------------------------------------------------------


def _anthropic_available() -> bool:
    if anthropic is None:
        return False
    if config.ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    # `ant auth login` stores a profile the SDK picks up with no env var set.
    return (
        os.path.isdir(os.path.expanduser("~/.config/anthropic"))
        or os.path.isdir(os.path.expanduser("~/.anthropic"))
    )


def _gemini_available() -> bool:
    return google_genai is not None and bool(config.GEMINI_API_KEY)


def provider() -> str:
    """The backend this run will use. Anthropic wins ties for back-compatibility."""
    forced = config.LLM_PROVIDER
    if forced in ("anthropic", "gemini"):
        return forced
    if _anthropic_available():
        return "anthropic"
    if _gemini_available():
        return "gemini"
    return "anthropic"  # the default; `available()` will report False


def available() -> bool:
    return _gemini_available() if provider() == "gemini" else _anthropic_available()


def model_name() -> str:
    return config.GEMINI_MODEL if provider() == "gemini" else config.ANTHROPIC_MODEL


def status() -> dict[str, Any]:
    active = provider()
    return {
        "available": available(),
        "provider": active,
        "model": model_name(),
        "sdk_installed": (google_genai is not None) if active == "gemini"
        else (anthropic is not None),
        "anthropic_ready": _anthropic_available(),
        "gemini_ready": _gemini_available(),
    }


def _no_credentials_error() -> LLMError:
    return LLMError(
        "No LLM credentials found. Set ANTHROPIC_API_KEY, or set GEMINI_API_KEY "
        "for Google's free tier (https://aistudio.google.com/apikey), in your .env "
        "file to enable resume tailoring, fit review, and interview prep."
    )


# --- public entry points ------------------------------------------------------


def complete(system: str, prompt: str, *, max_tokens: int = 16000,
             effort: str = "high") -> str:
    """Run a single completion and return its text."""
    if not available():
        raise _no_credentials_error()
    if provider() == "gemini":
        return _gemini_complete(system, prompt, max_tokens=max_tokens)
    return _anthropic_complete(system, prompt, max_tokens=max_tokens, effort=effort)


def complete_json(system: str, prompt: str, schema: dict[str, Any], *,
                  max_tokens: int = 8000, effort: str = "medium") -> Any:
    """Run a completion constrained to `schema` and return the parsed object."""
    if not available():
        raise _no_credentials_error()
    if provider() == "gemini":
        return _gemini_complete_json(system, prompt, schema, max_tokens=max_tokens)
    return _anthropic_complete_json(system, prompt, schema, max_tokens=max_tokens, effort=effort)


# --- Anthropic backend --------------------------------------------------------


def _get_client():
    global _client
    if anthropic is None:
        raise LLMError(
            "The `anthropic` package is not installed. Run: pip install -r requirements.txt"
        )
    if not _anthropic_available():
        raise _no_credentials_error()
    if _client is None:
        # Zero-arg constructor: resolves API key, auth token, or CLI profile.
        _client = anthropic.Anthropic()
    return _client


def _text_of(message) -> str:
    return "\n".join(block.text for block in message.content if block.type == "text").strip()


def _anthropic_complete(system: str, prompt: str, *, max_tokens: int, effort: str = "high") -> str:
    client = _get_client()
    try:
        with client.messages.stream(
            model=config.ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = stream.get_final_message()
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
        if anthropic is not None and isinstance(exc, anthropic.APIError):
            raise LLMError(f"Anthropic API error: {exc}") from exc
        raise LLMError(str(exc)) from exc

    if message.stop_reason == "refusal":
        raise LLMError("The model declined this request.")
    text = _text_of(message)
    if not text:
        raise LLMError("The model returned an empty response.")
    return text


def _anthropic_complete_json(system: str, prompt: str, schema: dict[str, Any], *,
                             max_tokens: int, effort: str = "medium") -> Any:
    client = _get_client()
    try:
        message = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        if anthropic is not None and isinstance(exc, anthropic.APIError):
            raise LLMError(f"Anthropic API error: {exc}") from exc
        raise LLMError(str(exc)) from exc

    if message.stop_reason == "refusal":
        raise LLMError("The model declined this request.")
    text = _text_of(message)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Model returned unparseable JSON: {text[:200]}") from exc


# --- Gemini backend -----------------------------------------------------------


def _get_gemini_client():
    global _gemini_client
    if google_genai is None:
        raise LLMError(
            "The `google-genai` package is not installed. Run: "
            "pip install google-genai"
        )
    if not config.GEMINI_API_KEY:
        raise _no_credentials_error()
    if _gemini_client is None:
        _gemini_client = google_genai.Client(api_key=config.GEMINI_API_KEY)
    return _gemini_client


def _gemini_text(response) -> str:
    """Gemini's `.text` is None when the model is blocked or returns no parts;
    dig out the reason so the UI shows something useful instead of a bare empty."""
    text = getattr(response, "text", None)
    if text:
        return text.strip()
    feedback = getattr(response, "prompt_feedback", None)
    blocked = getattr(feedback, "block_reason", None) if feedback else None
    if blocked:
        raise LLMError(f"Gemini blocked the request ({blocked}).")
    return ""


def _gemini_complete(system: str, prompt: str, *, max_tokens: int) -> str:
    client = _get_gemini_client()
    try:
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
            config=google_genai_types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
        raise LLMError(f"Gemini API error: {exc}") from exc

    text = _gemini_text(response)
    if not text:
        raise LLMError("Gemini returned an empty response.")
    return text


def _gemini_complete_json(system: str, prompt: str, schema: dict[str, Any], *,
                          max_tokens: int) -> Any:
    """Ask Gemini for JSON.

    Gemini's `response_schema` only accepts a subset of JSON Schema (no
    `additionalProperties`, limited nesting), and our schemas use the full set —
    so instead of translating them, we request `application/json` output and put
    the schema in the prompt as the contract. Robust across schema shapes, and
    the result is still parsed and validated by `json.loads`.
    """
    client = _get_gemini_client()
    contract = (
        f"{prompt}\n\nReturn ONLY a JSON object matching this schema exactly, "
        f"with no markdown fences or commentary:\n{json.dumps(schema)}"
    )
    try:
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=contract,
            config=google_genai_types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
                response_mime_type="application/json",
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"Gemini API error: {exc}") from exc

    text = _gemini_text(response)
    if not text:
        raise LLMError("Gemini returned an empty response.")
    try:
        return json.loads(_strip_json_fences(text))
    except json.JSONDecodeError as exc:
        raise LLMError(f"Gemini returned unparseable JSON: {text[:200]}") from exc


def _strip_json_fences(text: str) -> str:
    """Some models wrap JSON in ```json fences despite being told not to."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1] if "\n" in stripped else stripped
        stripped = stripped.rsplit("```", 1)[0]
    return stripped.strip()
