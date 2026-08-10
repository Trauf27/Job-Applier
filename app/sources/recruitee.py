"""Recruitee job boards.

Public endpoint, no key:
    https://{slug}.recruitee.com/api/offers/

Popular with European and remote-first companies, so it widens coverage beyond
the US-centric boards.
"""

from __future__ import annotations

from .base import RawJob, SourceError, get_json, html_to_text, looks_remote, normalize_timestamp

API = "https://{slug}.recruitee.com/api/offers/"


def fetch(slug: str) -> list[RawJob]:
    slug = slug.strip("/")
    payload = get_json(API.format(slug=slug))
    postings = payload.get("offers") if isinstance(payload, dict) else None
    if postings is None:
        raise SourceError(f"Unexpected Recruitee payload for '{slug}'")

    jobs: list[RawJob] = []
    for posting in postings:
        city = posting.get("city") or ""
        country = posting.get("country") or ""
        location = posting.get("location") or ", ".join(part for part in (city, country) if part)
        title = posting.get("title") or "Untitled role"
        remote = str(posting.get("remote") or "").lower() in ("true", "1", "yes") \
            or bool(posting.get("remote"))
        description = html_to_text(posting.get("description"))
        requirements = html_to_text(posting.get("requirements"))
        if requirements:
            description = f"{description}\n\n{requirements}".strip()
        jobs.append(
            RawJob(
                source="recruitee",
                source_job_id=str(posting.get("id")),
                company=posting.get("company_name") or slug,
                title=title,
                url=posting.get("careers_url") or posting.get("url") or "",
                location=location,
                remote=remote or looks_remote(location, title),
                description=description,
                posted_at=normalize_timestamp(posting.get("published_at") or posting.get("created_at")),
                extra={"department": posting.get("department")},
            )
        )
    return jobs
