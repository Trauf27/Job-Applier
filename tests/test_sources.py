from app.sources import ashby, base, companies, greenhouse, lever


def test_html_to_text_keeps_structure():
    html = "<p>We build things.</p><ul><li>Python</li><li>Go</li></ul><p>Apply&nbsp;now</p>"
    text = base.html_to_text(html)
    assert "We build things." in text
    assert "- Python" in text and "- Go" in text
    assert "<" not in text


def test_html_to_text_strips_scripts():
    assert "alert" not in base.html_to_text("<p>Hi</p><script>alert(1)</script>")


def test_looks_remote():
    assert base.looks_remote("Remote - US")
    assert not base.looks_remote("Austin, TX")


def test_normalize_timestamp_variants():
    assert base.normalize_timestamp(1700000000000).startswith("2023-")
    assert base.normalize_timestamp("2024-05-01T10:00:00Z").startswith("2024-05-01")
    assert base.normalize_timestamp(None) is None


def test_greenhouse_parsing(monkeypatch):
    payload = {
        "jobs": [{
            "id": 42,
            "title": "Senior Backend Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/42",
            "location": {"name": "Remote - US"},
            "content": "<p>Build payments with <b>Python</b>.</p>",
            "updated_at": "2024-05-01T10:00:00Z",
            "departments": [{"name": "Engineering"}],
        }]
    }
    monkeypatch.setattr(greenhouse, "get_json", lambda url, params=None: payload)
    jobs = greenhouse.fetch("acme")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "greenhouse" and job.source_job_id == "42"
    assert job.remote is True
    assert "Python" in job.description and "<p>" not in job.description


def test_lever_parsing_merges_list_sections(monkeypatch):
    payload = [{
        "id": "abc-123",
        "text": "Staff Engineer",
        "hostedUrl": "https://jobs.lever.co/acme/abc-123",
        "categories": {"location": "Austin, TX", "team": "Platform"},
        "descriptionPlain": "Own the platform.",
        "workplaceType": "onsite",
        "createdAt": 1700000000000,
        "lists": [{"text": "Requirements", "content": "<li>5 years Go</li>"}],
    }]
    monkeypatch.setattr(lever, "get_json", lambda url, params=None: payload)
    job = lever.fetch("acme")[0]
    assert job.source_job_id == "abc-123"
    assert job.remote is False
    assert "Requirements" in job.description and "5 years Go" in job.description


def test_ashby_parsing_appends_compensation(monkeypatch):
    payload = {"jobs": [{
        "id": "xyz",
        "title": "Product Engineer",
        "location": "New York",
        "jobUrl": "https://jobs.ashbyhq.com/acme/xyz",
        "descriptionPlain": "Ship product.",
        "isRemote": True,
        "publishedAt": "2024-06-01T00:00:00Z",
        "compensation": {"compensationTierSummary": "$180K – $220K"},
    }]}
    monkeypatch.setattr(ashby, "get_json", lambda url, params=None: payload)
    job = ashby.fetch("acme")[0]
    assert job.remote is True
    assert "$180K" in job.description


def test_detect_from_url():
    assert companies.detect_from_url("https://boards.greenhouse.io/acme/jobs/12345") == {
        "ats": "greenhouse", "slug": "acme", "name": "Acme"
    }
    assert companies.detect_from_url("https://job-boards.greenhouse.io/acme")["ats"] == "greenhouse"
    assert companies.detect_from_url("https://jobs.lever.co/acme/uuid-1")["ats"] == "lever"
    assert companies.detect_from_url("jobs.ashbyhq.com/acme/uuid-2")["ats"] == "ashby"
    assert companies.detect_from_url("https://example.com/careers") is None
    assert companies.detect_from_url("") is None
