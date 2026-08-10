"""SmartRecruiters job boards.

Public Posting API, no key:
    https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100

The list endpoint returns summaries without the full text, so each posting's
detail is fetched once for its description. Companies with hundreds of openings
would make that expensive, so the number of detail fetches is capped.
"""

from __future__ import annotations

from .base import RawJob, SourceError, get_json, html_to_text, looks_remote, normalize_timestamp

LIST_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
DETAIL_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}"

# How many postings to enrich with their full description per sync.
MAX_DETAIL_FETCHES = 60


def _location(posting: dict) -> tuple[str, bool]:
    loc = posting.get("location") or {}
    city = loc.get("city") or ""
    country = loc.get("country") or ""
    location = ", ".join(part for part in (city, country) if part)
    return location, bool(loc.get("remote"))


def _description(slug: str, posting_id: str) -> str:
    try:
        detail = get_json(DETAIL_API.format(slug=slug, posting_id=posting_id))
    except SourceError:
        return ""
    sections = (detail.get("jobAd") or {}).get("sections") or {}
    parts = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        text = html_to_text((sections.get(key) or {}).get("text"))
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def fetch(slug: str) -> list[RawJob]:
    slug = slug.strip("/")
    payload = get_json(LIST_API.format(slug=slug), params={"limit": 100})
    postings = payload.get("content") if isinstance(payload, dict) else None
    if postings is None:
        raise SourceError(f"Unexpected SmartRecruiters payload for '{slug}'")

    jobs: list[RawJob] = []
    for index, posting in enumerate(postings):
        posting_id = str(posting.get("id") or posting.get("uuid") or "")
        location, remote = _location(posting)
        title = posting.get("name") or "Untitled role"
        description = _description(slug, posting_id) if index < MAX_DETAIL_FETCHES else ""
        company = (posting.get("company") or {}).get("name") or slug
        jobs.append(
            RawJob(
                source="smartrecruiters",
                source_job_id=posting_id,
                company=company,
                title=title,
                url=f"https://jobs.smartrecruiters.com/{slug}/{posting_id}",
                location=location,
                remote=remote or looks_remote(location, title),
                description=description,
                posted_at=normalize_timestamp(posting.get("releasedDate") or posting.get("createdOn")),
                extra={},
            )
        )
    return jobs
