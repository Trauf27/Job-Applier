"""Open job feeds the agent is allowed to poll.

The company boards in `greenhouse`/`lever`/`ashby` only find roles at companies
you already thought to add. For the agent to *discover* jobs on a schedule it
needs a broad source it can legitimately call, which rules out LinkedIn and
Indeed. These two publish free, documented, key-less JSON APIs meant for
exactly this:

* **Remotive** — `remotive.com/api/remote-jobs`, remote roles, supports a search
  term. Their docs ask for one call at most every 24h per search, so keep the
  agent's interval sensible.
* **Arbeitnow** — `arbeitnow.com/api/job-board-api`, a paged board covering
  Europe-heavy and remote listings, filtered locally against your target titles.

Both are polled with the profile's `target_titles` as queries, so what comes
back is already roughly the shape of what you're looking for; local scoring then
ranks it the same way board postings are ranked.
"""

from __future__ import annotations

from .base import RawJob, SourceError, html_to_text, looks_remote, normalize_timestamp

REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
ARBEITNOW_URL = "https://www.arbeitnow.com/api/job-board-api"
JOBICY_URL = "https://jobicy.com/api/v2/remote-jobs"
REMOTEOK_URL = "https://remoteok.com/api"

# How many postings a single query may contribute, so one noisy feed can't
# swamp the job list.
PER_QUERY_LIMIT = 40


def _get_json(url: str, params: dict | None = None):
    from .base import get_json

    return get_json(url, params)


def _epoch_seconds(value) -> str | None:
    """Arbeitnow stamps `created_at` in epoch *seconds*, where the shared
    `normalize_timestamp` assumes milliseconds for a bare number — passing one to
    the other silently dates every posting to 1970."""
    try:
        return normalize_timestamp(int(value) * 1000)
    except (TypeError, ValueError):
        return normalize_timestamp(value)


def fetch_remotive(query: str = "", *, limit: int = PER_QUERY_LIMIT) -> list[RawJob]:
    params = {"limit": limit}
    if query:
        params["search"] = query
    payload = _get_json(REMOTIVE_URL, params)
    if not isinstance(payload, dict):
        raise SourceError("Remotive returned an unexpected payload")

    jobs = []
    for item in payload.get("jobs", [])[:limit]:
        description = html_to_text(item.get("description"))
        location = (item.get("candidate_required_location") or "").strip()
        jobs.append(
            RawJob(
                source="remotive",
                source_job_id=str(item.get("id") or item.get("url", "")),
                company=(item.get("company_name") or "").strip() or "Unknown company",
                title=(item.get("title") or "").strip() or "Untitled role",
                url=item.get("url") or "",
                location=location,
                remote=True,  # Remotive lists remote roles only.
                description=description,
                posted_at=normalize_timestamp(item.get("publication_date")),
                extra={"apply_url": item.get("url") or ""},
            )
        )
    return [job for job in jobs if job.source_job_id and job.url]


def fetch_arbeitnow(query: str = "", *, limit: int = PER_QUERY_LIMIT) -> list[RawJob]:
    """Arbeitnow has no search parameter, so the first page is filtered locally
    against the query terms."""
    payload = _get_json(ARBEITNOW_URL)
    if not isinstance(payload, dict):
        raise SourceError("Arbeitnow returned an unexpected payload")

    terms = [t for t in (query or "").lower().split() if len(t) > 2]
    jobs = []
    for item in payload.get("data", []):
        title = (item.get("title") or "").strip()
        if terms and not any(term in title.lower() for term in terms):
            continue
        description = html_to_text(item.get("description"))
        location = (item.get("location") or "").strip()
        jobs.append(
            RawJob(
                source="arbeitnow",
                source_job_id=str(item.get("slug") or item.get("url", "")),
                company=(item.get("company_name") or "").strip() or "Unknown company",
                title=title or "Untitled role",
                url=item.get("url") or "",
                location=location,
                remote=bool(item.get("remote")) or looks_remote(title, location),
                description=description,
                posted_at=_epoch_seconds(item.get("created_at")),
                extra={"apply_url": item.get("url") or ""},
            )
        )
        if len(jobs) >= limit:
            break
    return [job for job in jobs if job.source_job_id and job.url]


def fetch_jobicy(query: str = "", *, limit: int = PER_QUERY_LIMIT) -> list[RawJob]:
    params = {"count": limit}
    if query:
        params["tag"] = query  # Jobicy's keyword filter
    payload = _get_json(JOBICY_URL, params)
    if not isinstance(payload, dict):
        raise SourceError("Jobicy returned an unexpected payload")

    jobs = []
    for item in payload.get("jobs", [])[:limit]:
        location = (item.get("jobGeo") or "").strip()
        title = (item.get("jobTitle") or "").strip()
        description = html_to_text(item.get("jobDescription") or item.get("jobExcerpt"))
        jobs.append(
            RawJob(
                source="jobicy",
                source_job_id=str(item.get("id") or item.get("url", "")),
                company=(item.get("companyName") or "").strip() or "Unknown company",
                title=title or "Untitled role",
                url=item.get("url") or "",
                location=location,
                remote=True,  # Jobicy lists remote roles only.
                description=description,
                posted_at=normalize_timestamp(item.get("pubDate")),
                extra={"apply_url": item.get("url") or ""},
            )
        )
    return [job for job in jobs if job.source_job_id and job.url]


def fetch_remoteok(query: str = "", *, limit: int = PER_QUERY_LIMIT) -> list[RawJob]:
    """RemoteOK returns a JSON array whose first element is a legal/metadata
    notice, not a job — so it's skipped. There's no search parameter, so the
    query is applied locally against the title and tags."""
    payload = _get_json(REMOTEOK_URL)
    if not isinstance(payload, list):
        raise SourceError("RemoteOK returned an unexpected payload")

    terms = [t for t in (query or "").lower().split() if len(t) > 2]
    jobs = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("id"):
            continue  # the leading legal notice has no id
        title = (item.get("position") or "").strip()
        tags = " ".join(item.get("tags") or []).lower()
        haystack = f"{title} {tags}".lower()
        if terms and not any(term in haystack for term in terms):
            continue
        jobs.append(
            RawJob(
                source="remoteok",
                source_job_id=str(item.get("id")),
                company=(item.get("company") or "").strip() or "Unknown company",
                title=title or "Untitled role",
                url=item.get("url") or "",
                location=(item.get("location") or "").strip(),
                remote=True,  # RemoteOK lists remote roles only.
                description=html_to_text(item.get("description")),
                posted_at=normalize_timestamp(item.get("date") or item.get("epoch")),
                extra={"apply_url": item.get("apply_url") or item.get("url") or ""},
            )
        )
        if len(jobs) >= limit:
            break
    return [job for job in jobs if job.source_job_id and job.url]


FEEDS = {
    "remotive": fetch_remotive,
    "arbeitnow": fetch_arbeitnow,
    "jobicy": fetch_jobicy,
    "remoteok": fetch_remoteok,
}

FEED_CHOICES = sorted(FEEDS)


def fetch(feed: str, query: str = "", *, limit: int = PER_QUERY_LIMIT) -> list[RawJob]:
    try:
        fetcher = FEEDS[feed]
    except KeyError:
        raise SourceError(f"Unknown feed '{feed}'. Supported: {', '.join(FEED_CHOICES)}") from None
    return fetcher(query, limit=limit)


def search(feed: str, queries: list[str], *, limit: int = PER_QUERY_LIMIT) -> list[RawJob]:
    """Run every query against one feed and de-duplicate by posting id."""
    collected: dict[str, RawJob] = {}
    errors: list[str] = []
    for query in queries or [""]:
        try:
            for job in fetch(feed, query, limit=limit):
                collected.setdefault(job.source_job_id, job)
        except SourceError as exc:
            errors.append(str(exc))
    if errors and not collected:
        raise SourceError("; ".join(dict.fromkeys(errors)))
    return list(collected.values())
