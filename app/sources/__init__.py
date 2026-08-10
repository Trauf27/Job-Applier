"""Job sources.

Three kinds, in descending order of how well a machine can be trusted with them:

* **Company boards** (`greenhouse`, `lever`, `ashby`) — a company's own public
  JSON board. Full descriptions, no key, polite to poll.
* **Open feeds** (`feeds`) — free public search APIs the agent may poll on a
  schedule to *discover* companies you never listed.
* **LinkedIn** (`linkedin`) — never fetched; parsed out of alert emails, saved
  pages, or URLs you paste, because automated access breaks their terms.
"""

from __future__ import annotations

from .base import RawJob, SourceError, html_to_text
from . import ashby, feeds, greenhouse, lever, linkedin

FETCHERS = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
}

ATS_CHOICES = sorted(FETCHERS)
FEED_CHOICES = feeds.FEED_CHOICES


def fetch(ats: str, slug: str) -> list[RawJob]:
    try:
        fetcher = FETCHERS[ats]
    except KeyError:
        raise SourceError(f"Unknown ATS '{ats}'. Supported: {', '.join(ATS_CHOICES)}")
    return fetcher(slug)


__all__ = [
    "FETCHERS", "ATS_CHOICES", "FEED_CHOICES", "RawJob", "SourceError",
    "fetch", "feeds", "html_to_text", "linkedin",
]
