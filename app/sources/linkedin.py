"""LinkedIn ingestion.

LinkedIn's User Agreement forbids scraping and automated access, and their
anti-bot layer will lock an account that tries. So this module never talks to
linkedin.com. Instead it reads LinkedIn content **you already have**:

* a **job alert email** (forward it to yourself, "show original" / save as .html,
  or just select-all and paste the text),
* a **saved search page** you've opened in your own browser and copied,
* a list of **job URLs** you pasted, one per line.

`parse()` sniffs which of those it got and returns `RawJob` records. LinkedIn
postings rarely carry the full description in any of those forms, so imported
jobs score on title, company, location, and freshness; paste the description in
later — or better, use `apply_url` to follow the posting through to the
company's own ATS, which the agent can then poll properly.
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, unquote, urlparse

from .base import RawJob, html_to_text, looks_remote, normalize_timestamp

SOURCE = "linkedin"

# /jobs/view/4012345678, /comm/jobs/view/4012345678 (email links), and the
# ?currentJobId=4012345678 form used by the search and collections pages.
_VIEW_RE = re.compile(r"/jobs/view/(\d{6,})", re.I)
_CURRENT_ID_RE = re.compile(r"[?&]currentJobId=(\d{6,})", re.I)

_ANCHOR_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)

# Lines LinkedIn's UI interleaves with the actual job data. Dropping them is what
# makes a raw copy-paste of the results column parse into clean cards.
_NOISE_RE = re.compile(
    r"""^(?:
        promoted | easy\ apply | actively\ reviewing\ applicants | actively\ recruiting
      | viewed | saved | save(?:\ job)? | apply | applied | be\ an\ early\ applicant
      | \d+\ (?:connection|alum|school\ alum)s?\ works?\ here
      | (?:over\ )?\d+\ applicants?
      | reposted.* | \d+\ (?:second|minute|hour|day|week|month)s?\ ago(?:\ ·\ .*)?
      | (?:today|yesterday) | new | hybrid | on-?site | remote
      | \$[\d,.]+.* | .*\blogo\b.*
    )$""",
    re.I | re.X,
)

# "Acme Corp · San Francisco, CA (Remote)" — LinkedIn's combined subtitle line.
_SUBTITLE_RE = re.compile(r"^(?P<company>[^·]{2,80})·\s*(?P<location>.{2,120})$")

_RELATIVE_AGE_RE = re.compile(
    r"\b(\d+)\s+(second|minute|hour|day|week|month)s?\s+ago\b", re.I
)

_LOCATION_HINT_RE = re.compile(
    r"(remote|hybrid|on-?site|,\s*[A-Z]{2}\b|united states|united kingdom|canada|india|"
    r"germany|france|australia|singapore|netherlands|spain|ireland|poland|brazil|japan)",
    re.I,
)


def job_id_from_url(url: str) -> str | None:
    """Pull LinkedIn's numeric posting id out of any of its URL shapes."""
    if not url:
        return None
    match = _VIEW_RE.search(url) or _CURRENT_ID_RE.search(url)
    return match.group(1) if match else None


def canonical_url(job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def _strip_tags(fragment: str) -> str:
    return _clean(re.sub(r"<[^>]+>", " ", fragment or ""))


def _unwrap_redirect(url: str) -> str:
    """LinkedIn email links wrap the destination in a tracking redirect."""
    query = parse_qs(urlparse(url).query)
    for key in ("url", "u", "target"):
        if query.get(key):
            return unquote(query[key][0])
    return url


def _posted_at_from(text: str) -> str | None:
    """Turn "3 days ago" into an absolute timestamp so freshness scoring works."""
    match = _RELATIVE_AGE_RE.search(text or "")
    if not match:
        return None
    from datetime import datetime, timedelta, timezone

    amount = int(match.group(1))
    unit = match.group(2).lower()
    seconds = {
        "second": 1, "minute": 60, "hour": 3600,
        "day": 86400, "week": 604800, "month": 2592000,
    }[unit]
    stamp = datetime.now(timezone.utc) - timedelta(seconds=amount * seconds)
    return stamp.isoformat(timespec="seconds")


def _make_job(
    job_id: str,
    title: str,
    company: str,
    location: str = "",
    *,
    url: str | None = None,
    apply_url: str = "",
    description: str = "",
    posted_at: str | None = None,
) -> RawJob:
    return RawJob(
        source=SOURCE,
        source_job_id=job_id,
        company=company or "Unknown company",
        title=title or "Untitled role",
        url=url or canonical_url(job_id),
        location=location,
        remote=looks_remote(title, location, description),
        description=description,
        posted_at=normalize_timestamp(posted_at),
        extra={"apply_url": apply_url},
    )


# --- HTML (alert emails, saved pages) ----------------------------------------


def parse_html(source: str) -> list[RawJob]:
    """Read anchors pointing at job postings, then mine the text that follows each
    one for the company and location LinkedIn renders underneath the title."""
    text_only = _strip_tags(source)
    jobs: dict[str, RawJob] = {}

    for match in _ANCHOR_RE.finditer(source):
        href = _unwrap_redirect(html.unescape(match.group(1)))
        job_id = job_id_from_url(href)
        if not job_id:
            continue
        title = _strip_tags(match.group(2))
        if not title or job_id_from_url(title):
            continue

        # The company/location sit in the markup right after the title link.
        tail = _strip_tags(source[match.end(): match.end() + 600])
        company, location = _company_location_from_tail(tail)
        jobs.setdefault(
            job_id,
            _make_job(
                job_id, title, company, location,
                url=href.split("?")[0] or canonical_url(job_id),
                posted_at=_posted_at_from(tail),
            ),
        )

    # A page that links to postings without anchor text still gives us the ids.
    for job_id in _bare_ids(source):
        jobs.setdefault(job_id, _make_job(job_id, "", "", posted_at=_posted_at_from(text_only)))
    return list(jobs.values())


def _company_location_from_tail(tail: str) -> tuple[str, str]:
    """Best-effort split of the "Acme Corp · Austin, TX (Remote)" subtitle."""
    subtitle = _SUBTITLE_RE.match(tail[:160])
    if subtitle:
        location = subtitle.group("location")
        # Trim the trailing "2 days ago"/"Easy Apply" chatter LinkedIn appends.
        location = re.split(r"\s+(?:·|\d+\s+\w+\s+ago|Easy Apply|Promoted)", location)[0]
        return _clean(subtitle.group("company")), _clean(location)

    parts = [p for p in re.split(r"\s{2,}|·", tail) if _clean(p)]
    company = _clean(parts[0]) if parts else ""
    location = ""
    for part in parts[1:4]:
        if _LOCATION_HINT_RE.search(part):
            location = _clean(part)
            break
    return company[:80], location[:120]


def _bare_ids(source: str) -> list[str]:
    seen: list[str] = []
    for pattern in (_VIEW_RE, _CURRENT_ID_RE):
        for match in pattern.finditer(source):
            if match.group(1) not in seen:
                seen.append(match.group(1))
    return seen


# --- plain text ---------------------------------------------------------------


def parse_urls(source: str) -> list[RawJob]:
    """One posting per line. An optional pipe-delimited tail supplies the fields
    LinkedIn's URL alone can't: `<url> | Title | Company | Location`."""
    jobs: list[RawJob] = []
    seen: set[str] = set()
    for line in source.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        job_id = job_id_from_url(parts[0])
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)
        title, company, location = (parts[1:4] + ["", "", ""])[:3]
        jobs.append(_make_job(job_id, title, company, location))
    return jobs


def parse_cards(source: str) -> list[RawJob]:
    """Parse a copy-paste of LinkedIn's results column.

    Selecting the results list and copying gives repeated blocks shaped like:

        Senior Backend Engineer
        Acme Corp
        Austin, TX (Remote)
        Promoted · Easy Apply
        2 days ago

    Blank lines and the noise lines above end a card; the first three surviving
    lines are title, company, and location. Cards carry no URL, so they are
    keyed by a stable hash of their fields and are marked for manual linking.
    """
    cards: list[dict] = []
    current: list[str] = []

    def flush() -> None:
        nonlocal current
        if len(current) >= 2:
            cards.append({"fields": current[:3], "posted_at": None})
        current = []

    for raw_line in source.splitlines():
        line = _clean(raw_line)
        if not line:
            flush()
            continue
        if _NOISE_RE.match(line):
            flush()
            # "2 days ago" trails the card it belongs to.
            age = _posted_at_from(line)
            if age and cards:
                cards[-1]["posted_at"] = age
            continue
        if line in current:  # LinkedIn repeats the title for screen readers
            continue
        current.append(line)
        if len(current) == 3:
            flush()
    flush()

    jobs: list[RawJob] = []
    for card in cards:
        fields = card["fields"]
        title, company = fields[0], fields[1]
        location = fields[2] if len(fields) > 2 else ""
        if location and not _LOCATION_HINT_RE.search(location):
            # Third line wasn't a location — keep it out rather than guess.
            location = ""
        digest = re.sub(r"[^a-z0-9]+", "-", f"{title} {company} {location}".lower()).strip("-")
        jobs.append(
            _make_job(
                f"card-{digest[:80]}", title, company, location,
                url="https://www.linkedin.com/jobs/",
                posted_at=card["posted_at"],
            )
        )
    return jobs


# --- entry point --------------------------------------------------------------


def parse(source: str) -> list[RawJob]:
    """Turn any of the supported LinkedIn exports into `RawJob` records."""
    source = (source or "").strip()
    if not source:
        return []

    if "<a" in source.lower() and "href" in source.lower():
        jobs = parse_html(source)
        if jobs:
            return jobs

    if job_id_from_url(source):
        jobs = parse_urls(source)
        if jobs:
            return jobs
        # A single pasted URL buried in other text.
        ids = _bare_ids(source)
        if ids:
            return [_make_job(ids[0], "", "")]

    return parse_cards(html_to_text(source) if "<" in source else source)


def describe(jobs: list[RawJob]) -> dict[str, int]:
    """Counts the import endpoint reports back so the UI can say what was lost."""
    return {
        "total": len(jobs),
        "missing_title": sum(1 for j in jobs if j.title == "Untitled role"),
        "missing_company": sum(1 for j in jobs if j.company == "Unknown company"),
        "linked": sum(1 for j in jobs if "/jobs/view/" in j.url),
    }
