"""Shared plumbing for job-board sources."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .. import config


class SourceError(RuntimeError):
    """A board could not be fetched or parsed."""


@dataclass
class RawJob:
    source: str
    source_job_id: str
    company: str
    title: str
    url: str
    location: str = ""
    remote: bool = False
    description: str = ""
    posted_at: str | None = None
    extra: dict = field(default_factory=dict)


_REMOTE_RE = re.compile(r"\b(remote|distributed|work from home|anywhere)\b", re.I)

_BLOCK_TAGS = re.compile(r"</(p|div|li|ul|ol|h[1-6]|tr|table|section)\s*>", re.I)
_BR_TAGS = re.compile(r"<br\s*/?>", re.I)
_LI_TAGS = re.compile(r"<li[^>]*>", re.I)
_SCRIPTS = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
_BLANKS = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t]{2,}")


def html_to_text(raw: str | None) -> str:
    """Flatten an HTML job description into readable plain text.

    Job descriptions are the main LLM input, so structure (paragraphs, bullets)
    matters more than fidelity — we keep line breaks and bullet markers.
    """
    if not raw:
        return ""
    text = _SCRIPTS.sub(" ", raw)
    text = _BR_TAGS.sub("\n", text)
    text = _LI_TAGS.sub("\n- ", text)
    text = _BLOCK_TAGS.sub("\n", text)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANKS.sub("\n\n", text).strip()


def looks_remote(*fields: str) -> bool:
    return any(_REMOTE_RE.search(f or "") for f in fields)


def get_json(url: str, params: dict | None = None):
    headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    try:
        response = httpx.get(
            url,
            params=params,
            headers=headers,
            timeout=config.HTTP_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise SourceError(f"Network error fetching {url}: {exc}") from exc
    if response.status_code == 404:
        raise SourceError(f"Board not found (404): {url}")
    if response.status_code >= 400:
        raise SourceError(f"HTTP {response.status_code} fetching {url}")
    try:
        return response.json()
    except ValueError as exc:
        raise SourceError(f"Board did not return JSON: {url}") from exc


def iso_from_epoch_ms(value) -> str | None:
    try:
        seconds = int(value) / 1000
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")


def normalize_timestamp(value) -> str | None:
    """Accept the assorted date shapes boards emit; return ISO-8601 or None."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return iso_from_epoch_ms(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return iso_from_epoch_ms(text)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
