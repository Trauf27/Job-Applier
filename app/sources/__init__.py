"""Job sources.

Each source knows how to turn a company's public ATS board slug into a list of
`RawJob` records. All three supported boards expose free, key-less JSON APIs.
"""

from __future__ import annotations

from .base import RawJob, SourceError, html_to_text
from . import ashby, greenhouse, lever

FETCHERS = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
}

ATS_CHOICES = sorted(FETCHERS)


def fetch(ats: str, slug: str) -> list[RawJob]:
    try:
        fetcher = FETCHERS[ats]
    except KeyError:
        raise SourceError(f"Unknown ATS '{ats}'. Supported: {', '.join(ATS_CHOICES)}")
    return fetcher(slug)


__all__ = ["FETCHERS", "ATS_CHOICES", "RawJob", "SourceError", "fetch", "html_to_text"]
