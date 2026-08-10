"""Open feed parsing, with the HTTP layer stubbed out."""

import pytest

from app.sources import base, feeds

REMOTIVE_PAYLOAD = {
    "jobs": [
        {
            "id": 991,
            "title": "Senior Backend Engineer",
            "company_name": "Acme",
            "candidate_required_location": "USA Only",
            "url": "https://remotive.com/remote-jobs/backend/senior-backend-991",
            "description": "<p>We need <b>Python</b>.</p><ul><li>PostgreSQL</li></ul>",
            "publication_date": "2026-08-01T09:00:00",
        },
        # No URL: unusable, and must be dropped rather than stored broken.
        {"id": 992, "title": "Ghost", "company_name": "Nowhere", "url": ""},
    ]
}

ARBEITNOW_PAYLOAD = {
    "data": [
        {
            "slug": "backend-engineer-berlin-123",
            "title": "Backend Engineer",
            "company_name": "Globex",
            "location": "Berlin",
            "url": "https://www.arbeitnow.com/jobs/backend-engineer-berlin-123",
            "description": "<p>Go and Kubernetes</p>",
            "created_at": 1754006400,
            "remote": True,
        },
        {
            "slug": "marketing-lead-456",
            "title": "Marketing Lead",
            "company_name": "Initech",
            "location": "Munich",
            "url": "https://www.arbeitnow.com/jobs/marketing-lead-456",
            "description": "",
            "created_at": 1754006400,
            "remote": False,
        },
    ]
}


JOBICY_PAYLOAD = {
    "jobs": [{
        "id": 555,
        "jobTitle": "Senior Backend Engineer",
        "companyName": "Acme",
        "jobGeo": "Anywhere",
        "url": "https://jobicy.com/jobs/555-senior-backend-engineer",
        "jobDescription": "<p>Python and PostgreSQL.</p>",
        "pubDate": "2026-08-05 09:00:00",
    }]
}

REMOTEOK_PAYLOAD = [
    {"legal": "See https://remoteok.com/api for terms"},  # leading notice, not a job
    {
        "id": "778899",
        "position": "Backend Engineer",
        "company": "Globex",
        "location": "Worldwide",
        "tags": ["python", "postgres"],
        "description": "<p>Build APIs.</p>",
        "url": "https://remoteok.com/remote-jobs/778899",
        "date": "2026-08-06T00:00:00+00:00",
    },
    {
        "id": "112233",
        "position": "Marketing Manager",
        "company": "Initech",
        "tags": ["marketing"],
        "url": "https://remoteok.com/remote-jobs/112233",
        "description": "",
    },
]


@pytest.fixture()
def stub_http(monkeypatch):
    """Replace the network with a canned payload and record the calls made."""
    calls = []

    def fake_get_json(url, params=None):
        calls.append((url, params))
        if "remotive" in url:
            return REMOTIVE_PAYLOAD
        if "jobicy" in url:
            return JOBICY_PAYLOAD
        if "remoteok" in url:
            return REMOTEOK_PAYLOAD
        return ARBEITNOW_PAYLOAD

    monkeypatch.setattr(feeds, "_get_json", fake_get_json)
    return calls


def test_remotive_maps_fields(stub_http):
    jobs = feeds.fetch("remotive", "backend engineer")
    assert len(jobs) == 1  # the URL-less posting is dropped

    job = jobs[0]
    assert job.source == "remotive"
    assert job.source_job_id == "991"
    assert job.company == "Acme"
    assert job.remote is True
    assert job.posted_at.startswith("2026-08-01")
    # Descriptions arrive as HTML and have to be readable for the model.
    assert "PostgreSQL" in job.description and "<" not in job.description
    assert stub_http[0][1]["search"] == "backend engineer"


def test_arbeitnow_filters_locally_by_query(stub_http):
    jobs = feeds.fetch("arbeitnow", "backend")
    assert [job.title for job in jobs] == ["Backend Engineer"]
    # created_at is epoch seconds here, not the milliseconds the shared helper
    # assumes — get that wrong and every posting looks like it's from 1970,
    # which quietly zeroes the freshness component of the score.
    assert jobs[0].posted_at.startswith("2025-08-01")


def test_arbeitnow_without_a_query_returns_everything(stub_http):
    assert len(feeds.fetch("arbeitnow", "")) == 2


def test_search_deduplicates_across_queries(stub_http):
    jobs = feeds.search("remotive", ["backend engineer", "python engineer"])
    assert len(jobs) == 1
    assert len(stub_http) == 2  # both queries were actually run


def test_search_raises_only_when_every_query_failed(monkeypatch):
    def always_fails(url, params=None):
        raise base.SourceError("boom")

    monkeypatch.setattr(feeds, "_get_json", always_fails)
    with pytest.raises(base.SourceError):
        feeds.search("remotive", ["a", "b"])


def test_search_survives_a_partial_failure(monkeypatch):
    attempts = {"n": 0}

    def flaky(url, params=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise base.SourceError("transient")
        return REMOTIVE_PAYLOAD

    monkeypatch.setattr(feeds, "_get_json", flaky)
    assert len(feeds.search("remotive", ["a", "b"])) == 1


def test_jobicy_maps_fields(stub_http):
    jobs = feeds.fetch("jobicy", "backend")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "jobicy" and job.source_job_id == "555"
    assert job.remote is True
    assert "PostgreSQL" in job.description and "<" not in job.description
    # The query is passed as Jobicy's keyword tag.
    assert stub_http[0][1]["tag"] == "backend"


def test_remoteok_skips_the_legal_notice_and_filters(stub_http):
    jobs = feeds.fetch("remoteok", "python")
    # The leading notice is dropped; only the Python role matches the query.
    assert [job.title for job in jobs] == ["Backend Engineer"]
    assert jobs[0].source_job_id == "778899"
    assert jobs[0].remote is True


def test_remoteok_without_a_query_returns_all_real_jobs(stub_http):
    assert len(feeds.fetch("remoteok", "")) == 2


def test_all_feeds_are_registered():
    assert set(feeds.FEED_CHOICES) == {"remotive", "arbeitnow", "jobicy", "remoteok"}


def test_unknown_feed_is_rejected():
    with pytest.raises(base.SourceError):
        feeds.fetch("linkedin", "anything")
