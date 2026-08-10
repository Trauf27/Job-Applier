"""HTTP surface for the agent, the LinkedIn importer, and applying."""

import pytest
from fastapi.testclient import TestClient

from app import db, server

PROFILE = {
    "name": "Alex Doe",
    "email": "alex@example.com",
    "skills": ["Python"],
    "target_titles": ["Backend Engineer"],
    "experience": [{"company": "Acme", "title": "Backend Engineer",
                    "start": "2020", "end": "Present", "bullets": ["Built billing"]}],
}


@pytest.fixture()
def client(temp_db, monkeypatch):
    # The lifespan starts the scheduler thread; keep it out of the tests.
    monkeypatch.setattr(server.agent.scheduler, "start_in_background", lambda: None)
    with TestClient(server.app) as test_client:
        yield test_client


# --- settings -----------------------------------------------------------------


def test_status_advertises_the_agent(client):
    body = client.get("/api/status").json()
    assert "remotive" in body["feed_choices"]
    assert body["agent"]["enabled"] is False
    assert "autofill_available" in body


def test_settings_round_trip(client):
    body = client.get("/api/agent/settings").json()
    assert body["settings"]["dry_run"] is True
    assert "Dry run" in body["summary"]

    updated = client.put("/api/agent/settings", json={
        "min_score": 80, "max_applications_per_day": 4, "dry_run": False,
        "company_blocklist": ["Globex"],
    }).json()
    assert updated["settings"]["min_score"] == 80
    assert updated["settings"]["company_blocklist"] == ["Globex"]
    assert "stop, leaving applications at 'ready'" in updated["summary"]


def test_turning_on_auto_submit_requires_confirmation(client):
    response = client.put("/api/agent/settings", json={
        "dry_run": False, "auto_apply": True, "auto_submit": True})
    assert response.status_code == 400
    assert "confirm_auto_submit" in response.json()["detail"]

    ok = client.put("/api/agent/settings", json={
        "dry_run": False, "auto_apply": True, "auto_submit": True,
        "confirm_auto_submit": True})
    assert ok.status_code == 200
    assert ok.json()["settings"]["auto_submit"] is True


def test_blocklist_accepts_a_pasted_block_of_text(client):
    saved = client.put("/api/agent/settings",
                       json={"company_blocklist": "Globex\n\n  Initech  \n"}).json()
    assert saved["settings"]["company_blocklist"] == ["Globex", "Initech"]


# --- runs ---------------------------------------------------------------------


def test_run_endpoint_starts_a_dry_run_and_logs_it(client):
    client.put("/api/profile", json=PROFILE)
    client.put("/api/agent/settings", json={"dry_run": True, "sync_boards": False, "feeds": []})

    assert client.post("/api/agent/run", json={}).json()["started"] is True

    # The run happens on its own thread; it has no network to wait for.
    for _ in range(100):
        runs = client.get("/api/agent/runs").json()["runs"]
        if runs and runs[0]["finished_at"]:
            break
    assert runs[0]["status"] == "ok"
    assert runs[0]["dry_run"] is True

    detail = client.get(f"/api/agent/runs/{runs[0]['id']}").json()
    assert "actions" in detail and "stats" in detail


def test_unknown_run_is_a_404(client):
    assert client.get("/api/agent/runs/9999").status_code == 404


def test_stop_is_harmless_when_nothing_is_running(client):
    assert client.post("/api/agent/stop").json() == {"stopping": False}


# --- LinkedIn import ----------------------------------------------------------


LINKEDIN_PASTE = (
    "https://www.linkedin.com/jobs/view/4012345678/ | Senior Backend Engineer | Globex | Austin, TX\n"
    "https://www.linkedin.com/jobs/view/4099999999/ | Data Engineer | Initech | Remote US\n"
)


def test_linkedin_import_creates_scored_jobs(client):
    client.put("/api/profile", json=PROFILE)
    body = client.post("/api/linkedin/import", json={"text": LINKEDIN_PASTE}).json()

    assert body["added"] == 2
    assert body["parsed"]["total"] == 2
    assert body["parsed"]["missing_company"] == 0

    jobs = client.get("/api/jobs").json()
    assert {job["company"] for job in jobs} == {"Globex", "Initech"}
    # Imported postings are scored like everything else.
    assert all(job["score"] is not None for job in jobs)


def test_re_importing_refreshes_rather_than_duplicates(client):
    client.put("/api/profile", json=PROFILE)
    client.post("/api/linkedin/import", json={"text": LINKEDIN_PASTE})
    second = client.post("/api/linkedin/import", json={"text": LINKEDIN_PASTE}).json()
    assert second["added"] == 0 and second["updated"] == 2
    assert len(client.get("/api/jobs").json()) == 2


def test_import_never_blanks_a_description_it_already_had(client):
    client.put("/api/profile", json=PROFILE)
    client.post("/api/linkedin/import", json={"text": LINKEDIN_PASTE})
    job_id = client.get("/api/jobs").json()[0]["id"]
    db.execute("UPDATE jobs SET description = ? WHERE id = ?", ("Pasted by hand.", job_id))

    client.post("/api/linkedin/import", json={"text": LINKEDIN_PASTE})
    assert client.get(f"/api/jobs/{job_id}").json()["description"] == "Pasted by hand."


def test_unparseable_paste_gets_a_useful_error(client):
    response = client.post("/api/linkedin/import", json={"text": "just some prose about jobs"})
    assert response.status_code == 400
    assert "job-alert email" in response.json()["detail"]


def test_empty_paste_is_rejected(client):
    assert client.post("/api/linkedin/import", json={"text": ""}).status_code == 400


# --- feeds --------------------------------------------------------------------


def test_feed_sync_rejects_unknown_feeds(client):
    response = client.post("/api/feeds/sync", json={"feeds": ["linkedin"]})
    assert response.status_code == 400
    assert "linkedin" in response.json()["detail"]


# --- paste-any-job import -----------------------------------------------------


def test_paste_import_creates_a_scored_job(client):
    client.put("/api/profile", json=PROFILE)
    body = client.post("/api/jobs/import", json={"text": (
        "Title: Backend Engineer\nCompany: Systems Ltd\nLocation: Lahore, Pakistan\n"
        "URL: https://www.rozee.pk/job/999\n\nBuild Python services."
    )}).json()
    assert body["added"] == 1
    assert body["parsed"]["with_url"] == 1

    jobs = client.get("/api/jobs").json()
    assert jobs[0]["company"] == "Systems Ltd"
    assert jobs[0]["score"] is not None


def test_paste_import_rejects_structureless_text(client):
    response = client.post("/api/jobs/import", json={"text": "hello there, any jobs?"})
    assert response.status_code == 400
    assert "Title" in response.json()["detail"]


def test_paste_import_rejects_empty(client):
    assert client.post("/api/jobs/import", json={"text": ""}).status_code == 400


# --- applying -----------------------------------------------------------------


def _seed_job(client):
    client.put("/api/profile", json=PROFILE)
    client.post("/api/linkedin/import", json={"text": LINKEDIN_PASTE})
    return client.get("/api/jobs").json()[0]["id"]


def test_apply_needs_a_tailored_resume_first(client):
    job_id = _seed_job(client)
    attempt = client.post(f"/api/jobs/{job_id}/apply", json={}).json()
    assert attempt["status"] == "skipped"
    assert "No tailored resume" in attempt["detail"]


def test_dry_run_apply_resolves_answers_without_a_browser(client):
    job_id = _seed_job(client)
    job = db.row_to_dict(db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,)))
    from app import documents

    documents.save(job, "resume", "# Alex Doe\n\nBackend engineer.")

    attempt = client.post(f"/api/jobs/{job_id}/apply", json={"dry_run": True}).json()
    assert attempt["status"] == "prepared"
    filled = {item["label"] for item in attempt["filled"]}
    assert {"full_name", "email"} <= filled
    # The resume was rendered to a real PDF ready for upload.
    from pathlib import Path

    from app import config

    assert any(p.suffix == ".pdf" for p in Path(config.DOCS_DIR).iterdir())


def test_submitting_needs_an_explicit_confirmation(client):
    job_id = _seed_job(client)
    response = client.post(f"/api/jobs/{job_id}/apply", json={"submit": True})
    assert response.status_code == 400
    assert "confirm" in response.json()["detail"]


def test_attempts_are_listed_per_job(client):
    job_id = _seed_job(client)
    assert client.get(f"/api/jobs/{job_id}/attempts").json() == []
    client.post(f"/api/jobs/{job_id}/apply", json={})
    attempts = client.get(f"/api/jobs/{job_id}/attempts").json()
    assert len(attempts) == 1
    assert attempts[0]["job_id"] == job_id


def test_missing_screenshot_is_a_404(client):
    assert client.get("/api/attempts/1/screenshot").status_code == 404
