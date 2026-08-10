"""Workable job boards.

Public endpoint, no key:
    https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true

Workable powers a large share of small- and mid-size companies' career pages,
including many outside the US — which is the point of adding it here.
"""

from __future__ import annotations

from .base import RawJob, SourceError, get_json, html_to_text, looks_remote, normalize_timestamp

API = "https://apply.workable.com/api/v1/widget/accounts/{slug}"


def fetch(slug: str) -> list[RawJob]:
    payload = get_json(API.format(slug=slug.strip("/")), params={"details": "true"})
    postings = payload.get("jobs") if isinstance(payload, dict) else None
    if postings is None:
        raise SourceError(f"Unexpected Workable payload for '{slug}'")

    jobs: list[RawJob] = []
    for posting in postings:
        city = posting.get("city") or ""
        country = posting.get("country") or ""
        location = ", ".join(part for part in (city, country) if part)
        title = posting.get("title") or "Untitled role"
        remote = bool(posting.get("remote") or posting.get("telecommuting"))
        description = html_to_text(posting.get("description")) or (posting.get("shortcode") and "")
        jobs.append(
            RawJob(
                source="workable",
                source_job_id=str(posting.get("shortcode") or posting.get("id")),
                company=posting.get("account_name") or slug,
                title=title,
                url=posting.get("url") or posting.get("application_url") or "",
                location=location,
                remote=remote or looks_remote(location, title),
                description=description or "",
                posted_at=normalize_timestamp(posting.get("published_on") or posting.get("created_at")),
                extra={"department": posting.get("department")},
            )
        )
    return jobs
