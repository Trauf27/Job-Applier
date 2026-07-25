"""Ashby job boards.

Public endpoint, no key:
    https://api.ashbyhq.com/posting-api/job-board/{slug}
"""

from __future__ import annotations

from .base import RawJob, SourceError, get_json, html_to_text, looks_remote, normalize_timestamp

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def fetch(slug: str) -> list[RawJob]:
    payload = get_json(
        API.format(slug=slug.strip("/")),
        params={"includeCompensation": "true"},
    )
    postings = payload.get("jobs") if isinstance(payload, dict) else None
    if postings is None:
        raise SourceError(f"Unexpected Ashby payload for '{slug}'")

    jobs: list[RawJob] = []
    for posting in postings:
        location = posting.get("location") or ""
        title = posting.get("title") or "Untitled role"
        description = posting.get("descriptionPlain") or html_to_text(
            posting.get("descriptionHtml")
        )
        compensation = posting.get("compensation") or {}
        summary = compensation.get("compensationTierSummary")
        if summary:
            description += f"\n\nCompensation: {summary}"

        jobs.append(
            RawJob(
                source="ashby",
                source_job_id=str(posting.get("id")),
                company=slug,
                title=title,
                url=posting.get("jobUrl") or posting.get("applyUrl") or "",
                location=location,
                remote=bool(posting.get("isRemote")) or looks_remote(location, title),
                description=description.strip(),
                posted_at=normalize_timestamp(posting.get("publishedAt")),
                extra={
                    "team": posting.get("team"),
                    "employment_type": posting.get("employmentType"),
                },
            )
        )
    return jobs
