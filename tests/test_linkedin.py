"""LinkedIn ingestion: the three shapes a user can realistically paste."""

from app.sources import linkedin

ALERT_EMAIL = """
<html><body>
  <table><tr><td>
    <a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc&amp;refId=xyz">
      Senior Backend Engineer
    </a>
    <p>Acme Corp · Austin, TX (Remote)</p>
    <span>3 days ago</span>
  </td></tr>
  <tr><td>
    <a href="https://www.linkedin.com/comm/jobs/view/4099999999/?trackingId=def">
      Staff Platform Engineer
    </a>
    <p>Globex · San Francisco, CA</p>
    <span>1 week ago</span>
  </td></tr></table>
</body></html>
"""

COPIED_RESULTS = """
Senior Backend Engineer
Acme Corp
Austin, TX (Remote)
Promoted · Easy Apply
2 days ago

Staff Platform Engineer
Globex
San Francisco, CA
5 applicants
1 week ago
"""


def test_job_id_from_every_url_shape():
    assert linkedin.job_id_from_url("https://www.linkedin.com/jobs/view/4012345678/") == "4012345678"
    assert linkedin.job_id_from_url(
        "https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=x") == "4012345678"
    assert linkedin.job_id_from_url(
        "https://www.linkedin.com/jobs/search/?currentJobId=4012345678&start=25") == "4012345678"
    assert linkedin.job_id_from_url("https://www.linkedin.com/feed/") is None


def test_parse_alert_email():
    jobs = linkedin.parse(ALERT_EMAIL)
    assert len(jobs) == 2

    first = jobs[0]
    assert first.source == "linkedin"
    assert first.source_job_id == "4012345678"
    assert first.title == "Senior Backend Engineer"
    assert first.company == "Acme Corp"
    assert "Austin, TX" in first.location
    assert first.remote is True
    assert first.url == "https://www.linkedin.com/comm/jobs/view/4012345678/"
    # "3 days ago" becomes an absolute stamp so freshness scoring works.
    assert first.posted_at and first.posted_at.startswith("20")

    assert jobs[1].company == "Globex"
    assert jobs[1].remote is False


def test_parse_email_is_idempotent_on_repeated_links():
    doubled = ALERT_EMAIL + ALERT_EMAIL
    assert len({job.source_job_id for job in linkedin.parse(doubled)}) == 2


def test_parse_url_list_with_optional_fields():
    jobs = linkedin.parse(
        "https://www.linkedin.com/jobs/view/4011111111/ | Data Engineer | Initech | Remote US\n"
        "https://www.linkedin.com/jobs/view/4022222222/\n"
    )
    assert len(jobs) == 2
    assert jobs[0].title == "Data Engineer"
    assert jobs[0].company == "Initech"
    assert jobs[0].remote is True
    # A bare URL still imports; it just has nothing to score on yet.
    assert jobs[1].title == "Untitled role"
    assert jobs[1].company == "Unknown company"


def test_parse_copied_results_column():
    jobs = linkedin.parse(COPIED_RESULTS)
    assert len(jobs) == 2
    assert jobs[0].title == "Senior Backend Engineer"
    assert jobs[0].company == "Acme Corp"
    assert jobs[0].location == "Austin, TX (Remote)"
    assert jobs[1].title == "Staff Platform Engineer"
    # Cards carry no link, so they get a stable synthetic id instead.
    assert jobs[0].source_job_id.startswith("card-")
    assert jobs[0].source_job_id != jobs[1].source_job_id


def test_card_ids_are_stable_across_pastes():
    first = linkedin.parse(COPIED_RESULTS)
    second = linkedin.parse(COPIED_RESULTS)
    assert [j.source_job_id for j in first] == [j.source_job_id for j in second]


def test_noise_lines_never_become_jobs():
    jobs = linkedin.parse("Promoted\nEasy Apply\n2 days ago\nViewed\n")
    assert jobs == []


def test_empty_input():
    assert linkedin.parse("") == []
    assert linkedin.parse("   \n  ") == []


def test_describe_reports_what_is_missing():
    jobs = linkedin.parse("https://www.linkedin.com/jobs/view/4022222222/")
    summary = linkedin.describe(jobs)
    assert summary == {"total": 1, "missing_title": 1, "missing_company": 1, "linked": 1}
