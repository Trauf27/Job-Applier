from datetime import datetime, timedelta, timezone

from app import matching

PROFILE = {
    "skills": ["Python", "PostgreSQL", "Kubernetes", "Go", "Terraform"],
    "target_titles": ["Senior Backend Engineer", "Staff Software Engineer"],
    "target_locations": ["Austin"],
    "remote_ok": True,
    "seniority": "senior",
    "exclude_keywords": ["security clearance"],
}


def _job(**overrides):
    job = {
        "title": "Senior Backend Engineer",
        "description": "We use Python, PostgreSQL and Kubernetes to build payments infra.",
        "location": "Austin, TX",
        "remote": False,
        "posted_at": datetime.now(timezone.utc).isoformat(),
    }
    job.update(overrides)
    return job


def test_strong_match_scores_high():
    result = matching.score_job(_job(), PROFILE)
    assert result["score"] >= 80
    assert "Python" in result["matched_skills"]


def test_unrelated_title_scores_low():
    result = matching.score_job(
        _job(title="Warehouse Associate", description="Lift boxes. Operate a forklift."),
        PROFILE,
    )
    assert result["score"] < 30


def test_excluded_keyword_applies_penalty():
    clean = matching.score_job(_job(), PROFILE)
    flagged = matching.score_job(
        _job(description=_job()["description"] + " Must hold an active security clearance."),
        PROFILE,
    )
    assert flagged["score"] == max(0, clean["score"] - 25)
    assert any("security clearance" in reason for reason in flagged["reasons"])


def test_remote_job_matches_when_open_to_remote():
    result = matching.score_job(_job(location="Remote - US", remote=True), PROFILE)
    assert result["components"]["location"] == matching.WEIGHTS["location"]


def test_off_target_location_is_penalised():
    onsite = matching.score_job(_job(location="Berlin, Germany"), PROFILE)
    local = matching.score_job(_job(), PROFILE)
    assert onsite["score"] < local["score"]


def test_stale_posting_loses_freshness_points():
    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    stale = matching.score_job(_job(posted_at=old), PROFILE)
    assert stale["components"]["freshness"] == 0


def test_seniority_mismatch_penalised_by_distance():
    intern = matching.score_job(_job(title="Backend Engineering Intern"), PROFILE)
    assert any("Seniority mismatch" in reason for reason in intern["reasons"])
    assert intern["score"] < matching.score_job(_job(), PROFILE)["score"]


def test_detect_seniority_prefers_most_senior_signal():
    assert matching.detect_seniority("Senior Staff Engineer") == "staff"
    assert matching.detect_seniority("Engineering Manager") == "manager"
    assert matching.detect_seniority("Software Engineer") is None


def test_phrase_matching_respects_word_boundaries():
    result = matching.score_job(
        _job(description="We have ambitious goals and a Golang-free stack."),
        {"skills": ["Go"], "target_titles": [], "target_locations": []},
    )
    # "goals"/"Golang" must not count as a match for the skill "Go".
    assert result["matched_skills"] == []


def test_empty_profile_does_not_crash():
    result = matching.score_job(_job(), {})
    assert 0 <= result["score"] <= 100
