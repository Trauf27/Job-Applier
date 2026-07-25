"""Thin wrapper around the Anthropic Messages API.

Every LLM-backed feature degrades gracefully: if no credentials are configured,
`available()` is False and callers surface a clear message instead of raising.
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import config

try:  # The SDK is optional at import time so the app still boots without it.
    import anthropic
except ImportError:  # pragma: no cover - exercised only in stripped installs
    anthropic = None  # type: ignore[assignment]

_client = None


class LLMError(RuntimeError):
    """Raised when a model call cannot be made or completed."""


def available() -> bool:
    if anthropic is None:
        return False
    if config.ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    # `ant auth login` stores a profile the SDK picks up with no env var set.
    return (
        os.path.isdir(os.path.expanduser("~/.config/anthropic"))
        or os.path.isdir(os.path.expanduser("~/.anthropic"))
    )


def status() -> dict[str, Any]:
    return {
        "available": available(),
        "model": config.ANTHROPIC_MODEL,
        "sdk_installed": anthropic is not None,
    }


def _get_client():
    global _client
    if anthropic is None:
        raise LLMError(
            "The `anthropic` package is not installed. Run: pip install -r requirements.txt"
        )
    if not available():
        raise LLMError(
            "No Anthropic credentials found. Set ANTHROPIC_API_KEY in your .env "
            "file (or run `ant auth login`) to enable resume tailoring and interview prep."
        )
    if _client is None:
        # Zero-arg constructor: resolves API key, auth token, or CLI profile.
        _client = anthropic.Anthropic()
    return _client


def _text_of(message) -> str:
    return "\n".join(block.text for block in message.content if block.type == "text").strip()


def complete(
    system: str,
    prompt: str,
    *,
    max_tokens: int = 16000,
    effort: str = "high",
) -> str:
    """Run a single completion and return its text.

    Streams so that long generations (a full tailored resume, an interview prep
    pack) can't trip the SDK's request timeout.
    """
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


def complete_json(
    system: str,
    prompt: str,
    schema: dict[str, Any],
    *,
    max_tokens: int = 8000,
    effort: str = "medium",
) -> Any:
    """Run a completion constrained to `schema` and return the parsed object."""
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
