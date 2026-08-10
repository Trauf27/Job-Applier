"""The paste-any-job importer: fielded and freeform, one job or many."""

from app.sources import paste

FIELDED = """
Title: Senior Backend Engineer
Company: Systems Ltd
Location: Lahore, Pakistan
URL: https://www.rozee.pk/job/12345
Remote: no

We're hiring a backend engineer to work on our payments platform.
You'll build APIs in Python and PostgreSQL and own the billing service.
"""

TWO_JOBS = """
Title: Data Analyst
Company: Globex
Location: Karachi
Analyse sales data.
===SPLIT===
Title: Product Manager
Company: Initech
Location: Remote
Own the roadmap.
""".replace("===SPLIT===", "---")

FREEFORM = """
React Native Developer
Contour Software
Islamabad, Pakistan · On-site

We need a mobile engineer with 3+ years of React Native.
Apply at https://careers.contour.com/rn-dev
"""


def test_fielded_block_parses_every_field():
    jobs = paste.parse(FIELDED)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "paste"
    assert job.title == "Senior Backend Engineer"
    assert job.company == "Systems Ltd"
    assert job.location == "Lahore, Pakistan"
    assert job.url == "https://www.rozee.pk/job/12345"
    assert job.remote is False
    assert "payments platform" in job.description
    # A URL keys the job, so a re-paste updates rather than duplicates.
    assert job.source_job_id == "https://www.rozee.pk/job/12345"


def test_remote_field_is_understood():
    jobs = paste.parse("Title: X\nCompany: Y\nRemote: yes\n\nbody")
    assert jobs[0].remote is True


def test_remote_inferred_from_text_when_field_says_no():
    jobs = paste.parse("Title: X\nCompany: Y\nLocation: Remote - Anywhere\n\nWork from home role")
    assert jobs[0].remote is True


def test_several_jobs_split_on_a_rule():
    jobs = paste.parse(TWO_JOBS)
    assert [job.company for job in jobs] == ["Globex", "Initech"]
    assert jobs[1].remote is True


def test_freeform_guesses_title_company_location():
    jobs = paste.parse(FREEFORM)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "React Native Developer"
    assert job.company == "Contour Software"
    assert "Islamabad" in job.location
    # Any link in the body becomes the URL.
    assert job.url == "https://careers.contour.com/rn-dev"
    assert "React Native" in job.description


def test_label_aliases_are_accepted():
    jobs = paste.parse("Position: DevOps Engineer\nEmployer: Netsol\nCity: Lahore\n\nManage CI/CD.")
    job = jobs[0]
    assert job.title == "DevOps Engineer"
    assert job.company == "Netsol"
    assert job.location == "Lahore"


def test_a_bare_title_still_imports():
    jobs = paste.parse("Title: Graphic Designer")
    assert jobs[0].title == "Graphic Designer"
    assert jobs[0].company == "Unknown company"


def test_repasting_the_same_job_keeps_one_id():
    first = paste.parse(FIELDED)[0]
    second = paste.parse(FIELDED)[0]
    assert first.source_job_id == second.source_job_id


def test_no_title_or_company_yields_nothing():
    assert paste.parse("just some rambling prose with no structure at all") == []


def test_empty_input():
    assert paste.parse("") == []
    assert paste.parse("   \n  ") == []


def test_urlless_jobs_get_stable_hashed_ids():
    a = paste.parse("Title: A\nCompany: B\n\nbody")[0]
    b = paste.parse("Title: A\nCompany: B\n\ndifferent body")[0]
    # Same title+company => same id, so it updates rather than duplicating.
    assert a.source_job_id == b.source_job_id
    assert a.source_job_id.startswith("paste-")


def test_describe_summarises_the_batch():
    summary = paste.describe(paste.parse(FIELDED))
    assert summary["total"] == 1
    assert summary["with_url"] == 1
    assert summary["with_description"] == 1
    assert summary["missing_company"] == 0
