"""Lever job boards.

Public endpoint, no key:
    https://api.lever.co/v0/postings/{slug}?mode=json
"""

from __future__ import annotations

from .base import RawJob, SourceError, get_json, html_to_text, looks_remote, normalize_timestamp

API = "https://api.lever.co/v0/postings/{slug}"


def fetch(slug: str) -> list[RawJob]:
    payload = get_json(API.format(slug=slug.strip("/")), params={"mode": "json"})
    if not isinstance(payload, list):
        raise SourceError(f"Unexpected Lever payload for '{slug}'")

    jobs: list[RawJob] = []
    for posting in payload:
        categories = posting.get("categories") or {}
        location = categories.get("location") or ""
        workplace = posting.get("workplaceType") or ""
        title = posting.get("text") or "Untitled role"

        description = posting.get("descriptionPlain") or html_to_text(posting.get("description"))
        # Lever splits the body into "lists" (responsibilities, requirements...).
        for section in posting.get("lists") or []:
            heading = section.get("text") or ""
            body = html_to_text(section.get("content"))
            description += f"\n\n{heading}\n{body}" if heading else f"\n\n{body}"
        closing = posting.get("additionalPlain") or html_to_text(posting.get("additional"))
        if closing:
            description += f"\n\n{closing}"

        jobs.append(
            RawJob(
                source="lever",
                source_job_id=str(posting.get("id")),
                company=slug,
                title=title,
                url=posting.get("hostedUrl") or posting.get("applyUrl") or "",
                location=location,
                remote=workplace.lower() == "remote" or looks_remote(location, title),
                description=description.strip(),
                posted_at=normalize_timestamp(posting.get("createdAt")),
                extra={"team": categories.get("team"), "commitment": categories.get("commitment")},
            )
        )
    return jobs
