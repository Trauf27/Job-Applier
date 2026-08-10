"""The agent loop: policy, budgets, idempotence, and the audit log.

Every model call and every browser launch is stubbed. What's being tested is the
decision-making — which jobs it picks up, where it stops, and whether the log
explains itself.
"""

import pytest

from app import agent, db, pipeline
from app.agent import runner
from app.sources.base import RawJob

PROFILE = {
    "name": "Alex Doe",
    "email": "alex@example.com",
    "skills": ["Python", "PostgreSQL", "Kubernetes"],
    "target_titles": ["Backend Engineer", "Senior Backend Engineer"],
    "target_locations": ["Austin", "Remote"],
    "remote_ok": True,
    "seniority": "senior",
    "experience": [{"company": "Acme", "title": "Senior Backend Engineer",
                    "start": "2020", "end": "Present", "bullets": ["Built billing"]}],
}

# Nothing that reaches the network: no boards, no feeds.
OFFLINE = {"sync_boards": False, "feeds": []}


def _job(job_id: str, title="Senior Backend Engineer", company="Globex", **kwargs) -> RawJob:
    return RawJob(
        source="test", source_job_id=job_id, company=company, title=title,
        url=f"https://example.com/jobs/{job_id}", location="Austin, TX", remote=True,
        description="We need Python, PostgreSQL, and Kubernetes experience.",
        posted_at="2026-08-08T00:00:00+00:00", **kwargs,
    )


@pytest.fixture()
def seeded(temp_db):
    db.save_profile(PROFILE)
    pipeline.ingest([_job("a"), _job("b"), _job("c")])
    return temp_db


@pytest.fixture()
def fake_llm(monkeypatch):
    """A model that always likes the job, and counts how often it was called."""
    calls = {"review": 0, "resume": 0, "cover_letter": 0}

    def review(profile, job):
        calls["review"] += 1
        return {"score": 88, "verdict": "strong match", "strengths": ["Python"],
                "gaps": [], "talking_points": ["billing"], "resume_advice": "Lead with billing."}

    def resume(profile, job):
        calls["resume"] += 1
        return f"# {profile['name']}\n\nTailored for {job['title']}."

    def cover(profile, job):
        calls["cover_letter"] += 1
        return "Dear hiring team, ..."

    monkeypatch.setattr(runner.llm, "available", lambda: True)
    monkeypatch.setattr(runner.tailor, "deep_review", review)
    monkeypatch.setattr(runner.tailor, "tailored_resume", resume)
    monkeypatch.setattr(runner.tailor, "cover_letter", cover)
    return calls


def _stages(run, stage):
    return [action for action in run["actions"] if action["stage"] == stage]


# --- settings -----------------------------------------------------------------


def test_settings_defaults_are_conservative(temp_db):
    settings = agent.settings.get()
    assert settings["dry_run"] is True
    assert settings["auto_apply"] is False
    assert settings["auto_submit"] is False
    assert settings["max_applications_per_day"] <= 5


def test_settings_clamp_out_of_range_values(temp_db):
    saved = agent.settings.save({"max_applications_per_day": 9999, "interval_minutes": 1,
                                 "min_score": -20})
    assert saved["max_applications_per_day"] == 50
    assert saved["interval_minutes"] == 15
    assert saved["min_score"] == 0


def test_auto_submit_cannot_outrank_its_prerequisites(temp_db):
    # Submitting without filling in the form is meaningless...
    assert agent.settings.save({"auto_submit": True, "auto_apply": False})["auto_submit"] is False
    # ...and a dry run submits nothing by definition.
    saved = agent.settings.save({"auto_submit": True, "auto_apply": True, "dry_run": True})
    assert saved["auto_submit"] is False


def test_settings_ignore_unknown_keys(temp_db):
    assert "nonsense" not in agent.settings.save({"nonsense": 1})


# --- dry run ------------------------------------------------------------------


def test_dry_run_plans_without_spending_anything(seeded, fake_llm):
    run = runner.run_once(overrides={**OFFLINE, "dry_run": True, "min_score": 0})

    assert run["status"] == "ok"
    assert run["dry_run"] is True
    assert run["stats"]["shortlisted"] == 3
    assert len(_stages(run, "plan")) == 3
    # No model calls, no documents, no applications.
    assert fake_llm == {"review": 0, "resume": 0, "cover_letter": 0}
    assert db.query("SELECT * FROM documents") == []
    assert db.query("SELECT * FROM applications") == []


def test_dry_run_explains_an_empty_shortlist(temp_db, fake_llm):
    db.save_profile(PROFILE)
    pipeline.ingest([_job("z", title="Warehouse Associate", company="Initech")])
    run = runner.run_once(overrides={**OFFLINE, "dry_run": True, "min_score": 70})
    assert run["stats"]["shortlisted"] == 0
    assert "Lower the threshold" in " ".join(run["stats"]["notes"])


# --- the full loop ------------------------------------------------------------


def test_full_run_reviews_tailors_and_stops_at_ready(seeded, fake_llm):
    run = runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0,
                                     "auto_apply": False})

    assert run["stats"]["reviewed"] == 3
    assert run["stats"]["tailored"] == 3
    assert fake_llm["resume"] == 3 and fake_llm["cover_letter"] == 3

    statuses = {row["status"] for row in db.query("SELECT status FROM applications")}
    assert statuses == {"ready"}

    kinds = {row["kind"] for row in db.query("SELECT kind FROM documents")}
    assert kinds == {"review", "resume", "cover_letter"}

    # With auto_apply off, every job is explicitly logged as left for the human.
    assert all("auto_apply is off" in action["detail"] for action in _stages(run, "apply"))


def test_a_poor_fit_is_dropped_before_any_documents_are_written(seeded, monkeypatch):
    monkeypatch.setattr(runner.llm, "available", lambda: True)
    monkeypatch.setattr(runner.tailor, "deep_review", lambda profile, job: {
        "score": 20, "verdict": "skip", "strengths": [], "gaps": ["No Rust"],
        "talking_points": [], "resume_advice": "",
    })

    run = runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0})

    assert run["stats"]["passed_review"] == 0
    assert run["stats"]["tailored"] == 0
    assert {row["kind"] for row in db.query("SELECT kind FROM documents")} == {"review"}
    # The log says why, naming the gap.
    assert "No Rust" in _stages(run, "review")[0]["detail"]


def test_fit_below_the_threshold_is_skipped_even_with_a_kind_verdict(seeded, monkeypatch):
    monkeypatch.setattr(runner.llm, "available", lambda: True)
    monkeypatch.setattr(runner.tailor, "deep_review", lambda profile, job: {
        "score": 40, "verdict": "worth applying", "strengths": [], "gaps": [],
        "talking_points": [], "resume_advice": "",
    })
    run = runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0,
                                     "review_min_score": 65})
    assert run["stats"]["tailored"] == 0
    assert "under the 65 threshold" in _stages(run, "review")[0]["detail"]


# --- budgets ------------------------------------------------------------------


def test_review_budget_caps_model_calls(seeded, fake_llm):
    runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0,
                               "max_reviews_per_run": 2})
    assert fake_llm["review"] == 2


def test_tailoring_budget_stops_short_and_says_so(seeded, fake_llm):
    run = runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0,
                                     "max_documents_per_run": 1})
    assert run["stats"]["tailored"] == 1
    assert fake_llm["resume"] == 1
    skipped = [a for a in _stages(run, "tailor") if a["decision"] == "skip"]
    assert len(skipped) == 2
    assert "budget" in skipped[0]["detail"]


@pytest.fixture()
def fake_browser(monkeypatch):
    """Stub only the browser itself, so the real `apply.attempt` still runs —
    rendering the PDF, resolving answers, recording the attempt, and moving the
    application to 'applied'. Returns the calls the driver would have made."""
    from app.apply import browser

    calls = []

    def apply_to(url, values, *, auto_submit=False, **kwargs):
        calls.append({"url": url, "auto_submit": auto_submit, "values": values})
        return browser.AttemptResult(
            status="submitted" if auto_submit else "needs_review",
            form_url=url,
            filled=[{"label": "Email", "value": "alex@example.com", "source": "profile.email"}],
            detail="stubbed",
        )

    monkeypatch.setattr(browser, "apply_to", apply_to)
    return calls


def test_daily_application_cap_counts_what_already_went_out(seeded, fake_llm, fake_browser):
    run = runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0,
                                     "auto_apply": True, "auto_submit": True,
                                     "max_applications_per_run": 3,
                                     "max_applications_per_day": 2})

    # The third is held back: the first two already counted against today.
    assert len(fake_browser) == 2
    held = [a for a in _stages(run, "apply") if "Daily cap" in a["detail"]]
    assert len(held) == 1
    assert run["stats"]["submitted"] == 2


def test_the_daily_cap_survives_a_restart(seeded, fake_llm, fake_browser):
    """The cap is read from the database, so a second run can't top up the day."""
    runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0,
                               "auto_apply": True, "auto_submit": True,
                               "max_applications_per_run": 1,
                               "max_applications_per_day": 1})
    assert len(fake_browser) == 1

    runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0,
                               "auto_apply": True, "auto_submit": True,
                               "max_applications_per_run": 1,
                               "max_applications_per_day": 1})
    assert len(fake_browser) == 1  # still one — the earlier submit still counts


def test_a_submitted_application_lands_in_the_pipeline(seeded, fake_llm, fake_browser):
    runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0,
                               "auto_apply": True, "auto_submit": True,
                               "max_applications_per_run": 1, "max_applications_per_day": 1})

    applied = db.query("SELECT * FROM applications WHERE status = 'applied'")
    assert len(applied) == 1
    assert applied[0]["applied_at"]
    events = db.query("SELECT kind, detail FROM application_events WHERE application_id = ?",
                      (applied[0]["id"],))
    assert any("Submitted by the agent" in event["detail"] for event in events)


def test_per_run_application_budget(seeded, fake_llm, fake_browser):
    runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0,
                               "auto_apply": True, "max_applications_per_run": 1,
                               "max_applications_per_day": 10})
    assert len(fake_browser) == 1


def test_auto_submit_off_fills_but_never_sends(seeded, fake_llm, fake_browser):
    run = runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0,
                                     "auto_apply": True, "auto_submit": False,
                                     "max_applications_per_run": 3})
    assert len(fake_browser) == 3
    assert all(call["auto_submit"] is False for call in fake_browser)
    assert run["stats"]["submitted"] == 0
    assert db.query("SELECT * FROM applications WHERE status = 'applied'") == []
    assert all(a["decision"] == "needs_review" for a in _stages(run, "apply"))


def test_the_uploaded_resume_is_a_real_pdf(seeded, fake_llm, fake_browser):
    runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0,
                               "auto_apply": True, "max_applications_per_run": 1})
    from pathlib import Path

    resume_path = fake_browser[0]["values"].resume_path
    assert resume_path and Path(resume_path).read_bytes().startswith(b"%PDF")


# --- idempotence --------------------------------------------------------------


def test_a_second_run_does_not_re_review_the_same_jobs(seeded, fake_llm):
    runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0})
    assert fake_llm["review"] == 3

    second = runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0})
    assert fake_llm["review"] == 3  # unchanged
    assert second["stats"]["shortlisted"] == 0


def test_a_failed_review_is_retried_next_run(seeded, monkeypatch):
    from app.llm import LLMError

    monkeypatch.setattr(runner.llm, "available", lambda: True)
    monkeypatch.setattr(runner.tailor, "deep_review",
                        lambda profile, job: (_ for _ in ()).throw(LLMError("rate limited")))
    first = runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0})
    assert first["stats"]["errors"] == 3

    # A transient API failure must not blacklist the job for good.
    monkeypatch.setattr(runner.tailor, "deep_review", lambda profile, job: {
        "score": 90, "verdict": "strong match", "strengths": [], "gaps": [],
        "talking_points": [], "resume_advice": "",
    })
    monkeypatch.setattr(runner.tailor, "tailored_resume", lambda profile, job: "# Resume")
    monkeypatch.setattr(runner.tailor, "cover_letter", lambda profile, job: "Dear team")
    second = runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0})
    assert second["stats"]["reviewed"] == 3


def test_an_already_submitted_job_is_never_applied_to_again(seeded, fake_llm, fake_browser):
    # Run once with applying off: every job ends up tailored and 'ready'.
    runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0})
    job_id = db.query_one("SELECT id FROM jobs ORDER BY id LIMIT 1")["id"]
    db.execute(
        "INSERT INTO apply_attempts (job_id, method, status, created_at) VALUES (?,?,?,?)",
        (job_id, "autofill", "submitted", "2026-01-01T00:00:00+00:00"),
    )

    runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0,
                               "auto_apply": True, "max_applications_per_run": 5,
                               "max_applications_per_day": 5})

    urls = [call["url"] for call in fake_browser]
    job_url = db.query_one("SELECT url FROM jobs WHERE id = ?", (job_id,))["url"]
    assert job_url not in urls          # this one was skipped
    assert len(urls) == 2               # the other two still went out


def test_already_submitted_is_also_checked_at_apply_time(seeded):
    """A belt-and-braces guard, in case a job reaches the apply stage anyway."""
    job_id = db.query_one("SELECT id FROM jobs LIMIT 1")["id"]
    assert runner._already_submitted(job_id) is False

    db.execute(
        "INSERT INTO apply_attempts (job_id, method, status, created_at) VALUES (?,?,?,?)",
        (job_id, "autofill", "submitted", db.now()),
    )
    assert runner._already_submitted(job_id) is True


# --- picking up where the last run stopped ------------------------------------


def test_a_job_deferred_by_the_tailoring_budget_is_finished_next_run(seeded, fake_llm):
    small = {**OFFLINE, "dry_run": False, "min_score": 0, "max_documents_per_run": 1}
    first = runner.run_once(overrides=small)
    assert first["stats"]["tailored"] == 1
    assert fake_llm["review"] == 3  # all three were reviewed…
    assert fake_llm["resume"] == 1  # …but only one was written

    second = runner.run_once(overrides=small)
    assert second["stats"]["tailored"] == 1
    # Crucially, it did not pay for a second review of a job it already vetted.
    assert fake_llm["review"] == 3
    assert fake_llm["resume"] == 2

    third = runner.run_once(overrides=small)
    assert third["stats"]["tailored"] == 1
    assert fake_llm["review"] == 3 and fake_llm["resume"] == 3

    # Everything is now tailored, and nothing is left in any queue.
    fourth = runner.run_once(overrides=small)
    assert fourth["stats"]["tailored"] == 0
    assert fourth["stats"]["shortlisted"] == 0


def test_a_job_deferred_by_the_daily_cap_goes_out_later(seeded, fake_llm, fake_browser):
    capped = {**OFFLINE, "dry_run": False, "min_score": 0, "auto_apply": True,
              "auto_submit": True, "max_applications_per_run": 1,
              "max_applications_per_day": 1}
    runner.run_once(overrides=capped)
    assert len(fake_browser) == 1

    # Same day, cap reached: the queue holds, nothing extra goes out.
    runner.run_once(overrides=capped)
    assert len(fake_browser) == 1

    # A new day (simulated by ageing yesterday's application) releases the queue.
    db.execute("UPDATE applications SET applied_at = '2026-01-01T00:00:00+00:00' "
               "WHERE status = 'applied'")
    db.execute("UPDATE apply_attempts SET created_at = '2026-01-01T00:00:00+00:00'")
    runner.run_once(overrides=capped)
    assert len(fake_browser) == 2


def test_a_ready_application_is_not_re_tailored(seeded, fake_llm, fake_browser):
    """Documents already written are never regenerated, so turning applying on
    later doesn't pay the tailoring cost twice."""
    runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0})
    assert fake_llm["resume"] == 3

    runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0,
                               "auto_apply": True, "max_applications_per_run": 3,
                               "max_applications_per_day": 3})
    assert fake_llm["resume"] == 3   # unchanged
    assert len(fake_browser) == 3    # but all three applications went out


def test_a_job_you_hid_is_dropped_from_the_queue(seeded, fake_llm, fake_browser):
    runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0})
    db.execute("UPDATE jobs SET hidden = 1 WHERE id = (SELECT MIN(id) FROM jobs)")

    runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0,
                               "auto_apply": True, "max_applications_per_run": 5,
                               "max_applications_per_day": 5})
    assert len(fake_browser) == 2


# --- policy edges -------------------------------------------------------------


def test_blocklisted_companies_never_reach_the_model(temp_db, fake_llm):
    db.save_profile(PROFILE)
    pipeline.ingest([_job("x", company="Globex"), _job("y", company="Initech")])
    run = runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0,
                                     "company_blocklist": ["globex"]})
    assert run["stats"]["shortlisted"] == 1
    assert fake_llm["review"] == 1


def test_jobs_you_have_taken_over_are_left_alone(seeded, fake_llm):
    job_id = db.query_one("SELECT id FROM jobs LIMIT 1")["id"]
    application = db.get_or_create_application(job_id)
    db.set_application_status(application["id"], "interview")

    run = runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0})
    assert run["stats"]["shortlisted"] == 2
    assert db.query_one("SELECT status FROM applications WHERE id = ?",
                        (application["id"],))["status"] == "interview"


def test_hidden_jobs_are_ignored(seeded, fake_llm):
    db.execute("UPDATE jobs SET hidden = 1 WHERE source_job_id = 'a'")
    run = runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0})
    assert run["stats"]["shortlisted"] == 2


def test_without_a_profile_the_run_stops_and_explains(temp_db, fake_llm):
    run = runner.run_once(overrides={**OFFLINE, "dry_run": False})
    assert run["status"] == "ok"
    assert "Profile is empty" in " ".join(run["stats"]["notes"])


def test_without_credentials_it_still_saves_what_it_found(seeded, monkeypatch):
    monkeypatch.setattr(runner.llm, "available", lambda: False)
    run = runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0})

    assert run["stats"]["reviewed"] == 0
    assert {row["status"] for row in db.query("SELECT status FROM applications")} == {"saved"}
    assert "ANTHROPIC_API_KEY" in " ".join(run["stats"]["notes"])


# --- follow-ups ---------------------------------------------------------------


def test_quiet_applications_are_flagged_once(seeded, fake_llm):
    job_id = db.query_one("SELECT id FROM jobs LIMIT 1")["id"]
    application = db.get_or_create_application(job_id)
    db.execute(
        "UPDATE applications SET status = 'applied', applied_at = ? WHERE id = ?",
        ("2026-01-01T00:00:00+00:00", application["id"]),
    )

    first = runner.run_once(overrides={**OFFLINE, "dry_run": True, "followup_days": 10})
    assert first["stats"]["followups"] == 1

    second = runner.run_once(overrides={**OFFLINE, "dry_run": True, "followup_days": 10})
    assert second["stats"]["followups"] == 0  # not nagged twice

    events = db.query(
        "SELECT kind FROM application_events WHERE application_id = ?", (application["id"],))
    assert sum(1 for event in events if event["kind"] == "followup_due") == 1


# --- run bookkeeping ----------------------------------------------------------


def test_runs_are_recorded_and_readable(seeded, fake_llm):
    run = runner.run_once(trigger="cli", overrides={**OFFLINE, "dry_run": True})
    stored = runner.get_run(run["id"])
    assert stored["trigger"] == "cli"
    assert stored["finished_at"]
    assert stored["status"] == "ok"
    assert runner.recent_runs()[0]["id"] == run["id"]


def test_a_crash_mid_run_still_closes_the_run(seeded, monkeypatch):
    monkeypatch.setattr(runner, "_scan",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    run = runner.run_once(overrides={**OFFLINE, "dry_run": True})
    assert run["status"] == "error"
    assert "boom" in run["error"]
    assert run["finished_at"]
    assert not runner.is_running()


def test_actions_carry_the_job_they_refer_to(seeded, fake_llm):
    run = runner.run_once(overrides={**OFFLINE, "dry_run": False, "min_score": 0})
    reviews = _stages(run, "review")
    assert all(action["job_id"] for action in reviews)
    assert all(action["title"] for action in reviews)
