"""Import a job from anywhere, by pasting it.

Pakistan's main boards — Rozee.pk, Mustakbil — and Google's job search don't
publish clean public APIs, and scraping them is fragile and often against their
terms. So this is the universal fallback: copy a posting from any site, email,
or message and paste it in. Two shapes are accepted.

**Fielded** — the reliable one. Label lines the parser recognises, then a blank
line, then the description:

    Title: Senior Backend Engineer
    Company: Systems Ltd
    Location: Lahore, Pakistan
    URL: https://www.rozee.pk/job/12345
    Remote: no

    <the rest is the job description>

To import several at once, separate them with a line of `---`.

**Freeform** — best-effort, for a rough copy-paste with no labels. Needs at least
two lines: the first is taken as the title, the second as the company, and a line
that looks like a place as the location. Good enough to score and track; add
labels if it guesses wrong. Any `http(s)://` link in the text becomes the URL.

Nothing is fetched. The description is whatever you pasted, which is exactly what
tailoring and scoring need.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from .base import RawJob, looks_remote, normalize_timestamp

SOURCE = "paste"

_URL_RE = re.compile(r"https?://[^\s)>\]]+", re.I)
_SEPARATOR_RE = re.compile(r"^\s*-{3,}\s*$", re.M)

# "Label: value" lines the fielded format understands. Order is irrelevant.
_FIELD_ALIASES = {
    "title": {"title", "job title", "position", "role", "job"},
    "company": {"company", "employer", "organisation", "organization", "company name"},
    "location": {"location", "city", "based in", "job location", "place"},
    "url": {"url", "link", "apply", "apply link", "apply url", "job url", "source"},
    "remote": {"remote", "workplace", "work type", "work mode"},
    "posted": {"posted", "date", "posted on", "published", "date posted"},
}
_ALIAS_TO_FIELD = {alias: field for field, aliases in _FIELD_ALIASES.items() for alias in aliases}

_LABEL_RE = re.compile(r"^\s*([A-Za-z][A-Za-z /]{1,24}?)\s*[:\-–]\s*(.+?)\s*$")

_LOCATION_HINT_RE = re.compile(
    r"remote|hybrid|on-?site|,\s*[A-Za-z]{2,}\b|pakistan|lahore|karachi|islamabad|"
    r"rawalpindi|remote|united|india|dubai|uae|singapore|london|new york|anywhere",
    re.I,
)
_REMOTE_WORDS = re.compile(r"\b(remote|anywhere|work from home|wfh)\b", re.I)


def _stable_id(title: str, company: str, url: str) -> str:
    """A URL keys the job when present; otherwise a hash of title+company, so the
    same posting pasted twice updates rather than duplicates."""
    if url:
        return url.split("?")[0].rstrip("/")
    digest = hashlib.sha1(f"{title}|{company}".lower().encode("utf-8")).hexdigest()
    return f"paste-{digest[:16]}"


def _first_url(text: str) -> str:
    match = _URL_RE.search(text or "")
    return match.group(0) if match else ""


def _looks_like_location(line: str) -> bool:
    return bool(_LOCATION_HINT_RE.search(line)) and len(line) <= 80


def _to_remote(value: str, *fallback: str) -> bool:
    value = (value or "").strip().lower()
    if value in ("yes", "true", "remote", "1", "y"):
        return True
    if value in ("no", "false", "onsite", "on-site", "0", "n"):
        return _REMOTE_WORDS.search(" ".join(fallback)) is not None
    return looks_remote(value, *fallback)


def _build(fields: dict[str, str], description: str) -> RawJob | None:
    title = (fields.get("title") or "").strip()
    company = (fields.get("company") or "").strip()
    if not title and not company:
        return None

    url = (fields.get("url") or "").strip() or _first_url(description)
    if url and "://" not in url:
        url = "https://" + url

    remote = _to_remote(fields.get("remote", ""), title, fields.get("location", ""), description)
    return RawJob(
        source=SOURCE,
        source_job_id=_stable_id(title, company, url),
        company=company or "Unknown company",
        title=title or "Untitled role",
        url=url or "",
        location=(fields.get("location") or "").strip(),
        remote=remote,
        description=description.strip(),
        posted_at=normalize_timestamp(fields.get("posted")),
        extra={"apply_url": url or "", "host": urlparse(url).hostname or ""},
    )


def _parse_block(block: str) -> RawJob | None:
    """One posting. Reads leading `Label: value` lines; the remainder, plus any
    unrecognised lines, becomes the description."""
    fields: dict[str, str] = {}
    description_lines: list[str] = []
    in_body = False

    for line in block.splitlines():
        if in_body:
            description_lines.append(line)
            continue
        match = _LABEL_RE.match(line)
        key = _ALIAS_TO_FIELD.get(match.group(1).strip().lower()) if match else None
        if key and key not in fields:
            fields[key] = match.group(2).strip()
        elif not line.strip():
            # A blank line after the labels marks the start of the body.
            if fields:
                in_body = True
        else:
            # A non-label line ends the header and starts the body.
            in_body = True
            description_lines.append(line)

    if fields:
        return _build(fields, "\n".join(description_lines))
    return _parse_freeform(block)


def _parse_freeform(block: str) -> RawJob | None:
    """No labels: guess title/company/location from the first lines.

    Requires at least two non-empty lines and a plausibly title-like first line —
    a single sentence of prose is not a job posting and should not become one.
    """
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    if len(lines) < 2 or len(lines[0]) > 120:
        return None

    fields: dict[str, str] = {"title": lines[0]}
    body_start = 1
    if len(lines) > 1 and not _looks_like_location(lines[1]):
        fields["company"] = lines[1]
        body_start = 2
    for line in lines[body_start:body_start + 3]:
        if _looks_like_location(line):
            fields["location"] = line
            break

    return _build(fields, "\n".join(lines[body_start:]))


def parse(text: str) -> list[RawJob]:
    """Turn pasted text into `RawJob` records — one, or several split by `---`."""
    text = (text or "").strip()
    if not text:
        return []

    blocks = [b for b in _SEPARATOR_RE.split(text) if b.strip()]
    jobs: list[RawJob] = []
    seen: set[str] = set()
    for block in blocks:
        job = _parse_block(block)
        if job and job.source_job_id not in seen:
            seen.add(job.source_job_id)
            jobs.append(job)
    return jobs


def describe(jobs: list[RawJob]) -> dict[str, int]:
    return {
        "total": len(jobs),
        "missing_title": sum(1 for j in jobs if j.title == "Untitled role"),
        "missing_company": sum(1 for j in jobs if j.company == "Unknown company"),
        "with_url": sum(1 for j in jobs if j.url),
        "with_description": sum(1 for j in jobs if len(j.description) > 40),
    }
