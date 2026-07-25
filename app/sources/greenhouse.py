"""Greenhouse job boards.

Public endpoint, no key:
    https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
"""

from __future__ import annotations

from .base import RawJob, SourceError, get_json, html_to_text, looks_remote, normalize_timestamp

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


def fetch(slug: str) -> list[RawJob]:
    payload = get_json(API.format(slug=slug.strip("/")), params={"content": "true"})
    postings = payload.get("jobs") if isinstance(payload, dict) else None
    if postings is None:
        raise SourceError(f"Unexpected Greenhouse payload for '{slug}'")

    jobs: list[RawJob] = []
    for posting in postings:
        location = (posting.get("location") or {}).get("name", "") or ""
        offices = ", ".join(
            office.get("name", "") for office in posting.get("offices") or [] if office.get("name")
        )
        description = html_to_text(posting.get("content"))
        title = posting.get("title") or "Untitled role"
        jobs.append(
            RawJob(
                source="greenhouse",
                source_job_id=str(posting.get("id")),
                company=slug,
                title=title,
                url=posting.get("absolute_url") or "",
                location=location or offices,
                remote=looks_remote(location, offices, title),
                description=description,
                posted_at=normalize_timestamp(
                    posting.get("first_published") or posting.get("updated_at")
                ),
                extra={"departments": [d.get("name") for d in posting.get("departments") or []]},
            )
        )
    return jobs
