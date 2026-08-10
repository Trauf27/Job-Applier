"""Company board helpers: URL detection and starter suggestions.

The reliable way to add a company is to paste one of its job URLs and let
`detect_from_url` pull out the ATS and board slug. The suggestions list is a
convenience only — companies migrate between ATS vendors, so anything that
404s on sync is reported per-company in the sync results and can be removed.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

PATTERNS = [
    # https://boards.greenhouse.io/acme/jobs/123  |  https://job-boards.greenhouse.io/acme
    (re.compile(r"(?:job-boards|boards)\.greenhouse\.io/([^/?#]+)", re.I), "greenhouse"),
    (re.compile(r"boards-api\.greenhouse\.io/v1/boards/([^/?#]+)", re.I), "greenhouse"),
    # https://jobs.lever.co/acme/uuid
    (re.compile(r"jobs\.(?:eu\.)?lever\.co/([^/?#]+)", re.I), "lever"),
    (re.compile(r"api\.lever\.co/v0/postings/([^/?#]+)", re.I), "lever"),
    # https://jobs.ashbyhq.com/acme/uuid
    (re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)", re.I), "ashby"),
    (re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([^/?#]+)", re.I), "ashby"),
    # https://apply.workable.com/acme/  |  https://acme.workable.com/
    (re.compile(r"apply\.workable\.com/([^/?#]+)", re.I), "workable"),
    (re.compile(r"([a-z0-9-]+)\.workable\.com", re.I), "workable"),
    # https://jobs.smartrecruiters.com/Acme  |  https://careers.smartrecruiters.com/Acme
    (re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([^/?#]+)", re.I), "smartrecruiters"),
    (re.compile(r"api\.smartrecruiters\.com/v1/companies/([^/?#]+)", re.I), "smartrecruiters"),
    # https://acme.recruitee.com/
    (re.compile(r"([a-z0-9-]+)\.recruitee\.com", re.I), "recruitee"),
]


def detect_from_url(url: str) -> dict[str, str] | None:
    """Extract {ats, slug, name} from a job or board URL, or None if unrecognised."""
    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url

    for pattern, ats in PATTERNS:
        match = pattern.search(url)
        if match:
            slug = match.group(1).strip("/")
            # Guard against matching a subdomain or path segment that is really a
            # route ("apply.workable.com" -> "apply", not a company).
            if slug.lower() in {"jobs", "careers", "apply", "embed", "www", "api",
                                "v1", "v0", "boards"}:
                continue
            return {"ats": ats, "slug": slug, "name": slug.replace("-", " ").title()}

    host = (urlparse(url).hostname or "").lower()
    if host.endswith("greenhouse.io") or host.endswith("lever.co") or host.endswith("ashbyhq.com"):
        return None
    return None


# Starter list. Verify by syncing — a wrong slug simply reports "Board not found".
SUGGESTIONS: list[dict[str, str]] = [
    {"name": "Anthropic", "ats": "greenhouse", "slug": "anthropic"},
    {"name": "Stripe", "ats": "greenhouse", "slug": "stripe"},
    {"name": "Databricks", "ats": "greenhouse", "slug": "databricks"},
    {"name": "Figma", "ats": "greenhouse", "slug": "figma"},
    {"name": "Airbnb", "ats": "greenhouse", "slug": "airbnb"},
    {"name": "Coinbase", "ats": "greenhouse", "slug": "coinbase"},
    {"name": "DoorDash", "ats": "greenhouse", "slug": "doordash"},
    {"name": "Reddit", "ats": "greenhouse", "slug": "reddit"},
    {"name": "Discord", "ats": "greenhouse", "slug": "discord"},
    {"name": "Instacart", "ats": "greenhouse", "slug": "instacart"},
    {"name": "Brex", "ats": "greenhouse", "slug": "brex"},
    {"name": "Samsara", "ats": "greenhouse", "slug": "samsara"},
    {"name": "Netflix", "ats": "lever", "slug": "netflix"},
    {"name": "Plaid", "ats": "lever", "slug": "plaid"},
    {"name": "Ramp", "ats": "ashby", "slug": "ramp"},
    {"name": "Linear", "ats": "ashby", "slug": "linear"},
    {"name": "Vercel", "ats": "ashby", "slug": "vercel"},
    {"name": "Replit", "ats": "ashby", "slug": "replit"},
]
