"""End-to-end tests over the HTTP API with a temporary database."""

import pytest
from fastapi.testclient import TestClient

from app import pipeline, server
from app.sources.base import RawJob


@pytest.fixture()
def client(temp_db):
    with TestClient(server.app) as test_client:
        yield test_client


PROFILE = {
    "name": "Alex Doe",
    "email": "alex@example.com",
    "skills": ["Python", "PostgreSQL"],
    "target_titles": ["Backend Engineer"],
    "target_locations": ["Austin"],
    "remote_ok": True,
    "experience": [{"company": "Acme", "title": "Backend Engineer",
                    "start": "2020", "end": "Present", "bullets": ["Built the billing service"]}],
}


def test_status_and_empty_state(client):
    body = client.get("/api/status").json()
    assert body["profile_ready"] is False
    assert "greenhouse" in body["ats_choices"]
    assert client.get("/api/jobs").json() == []


def test_profile_round_trip(client):
    saved = client.put("/api/profile", json=PROFILE).json()
    assert saved["name"] == "Alex Doe"
    assert saved["skills"] == ["Python", "PostgreSQL"]
    # Unknown keys are dropped rather than persisted.
    client.put("/api/profile", json={**PROFILE, "bogus": "x"})
    assert "bogus" not in client.get("/api/profile").json()


def test_company_validation(client):
    assert client.post("/api/companies", json={"ats": "workday", "slug": "acme"}).status_code == 400
    assert client.post("/api/companies", json={"ats": "lever", "slug": ""}).status_code == 400

    created = client.post("/api/companies", json={"name": "Acme", "ats": "lever", "slug": "acme"})
    assert created.status_code == 200
    # Duplicates are rejected rather than silently added twice.
    assert client.post("/api/companies", json={"ats": "lever", "slug": "acme"}).status_code == 409

    company_id = created.json()["id"]
    client.patch(f"/api/companies/{company_id}", json={"enabled": False})
    assert client.get("/api/companies").json()[0]["enabled"] is False
    client.delete(f"/api/companies/{company_id}")
    assert client.get("/api/companies").json() == []


def test_detect_endpoint(client):
    ok = client.post("/api/companies/detect", json={"url": "https://jobs.lever.co/acme/x"})
    assert ok.json() == {"ats": "lever", "slug": "acme", "name": "Acme"}
    assert client.post("/api/companies/detect", json={"url": "nope"}).status_code == 400


def _seed_jobs(client, monkeypatch):
    client.put("/api/profile", json=PROFILE)
    client.post("/api/companies", json={"name": "Acme", "ats": "lever", "slug": "acme"})

    raw = [
        RawJob(source="lever", source_job_id="1", company="acme", title="Backend Engineer",
               url="https://jobs.lever.co/acme/1", location="Austin, TX",
               description="Python and PostgreSQL all day.", posted_at="2099-01-01T00:00:00+00:00"),
        RawJob(source="lever", source_job_id="2", company="acme", title="Warehouse Associate",
               url="https://jobs.lever.co/acme/2", location="Reno, NV",
               description="Operate a forklift."),
    ]
    monkeypatch.setattr(pipeline.sources, "fetch", lambda ats, slug: raw)
    return client.post("/api/sync").json()


def test_sync_scores_and_dedupes(client, monkeypatch):
    first = _seed_jobs(client, monkeypatch)
    assert first["added"] == 2 and first["errors"] == 0

    # Re-syncing the same board updates rather than duplicating.
    second = client.post("/api/sync").json()
    assert second["added"] == 0 and second["updated"] == 2

    jobs = client.get("/api/jobs").json()
    assert [job["title"] for job in jobs] == ["Backend Engineer", "Warehouse Associate"]
    assert jobs[0]["score"] > jobs[1]["score"]
    assert jobs[0]["score_detail"]["matched_skills"] == ["Python", "PostgreSQL"]


def test_sync_reports_board_errors_without_failing(client, monkeypatch):
    client.post("/api/companies", json={"name": "Broken", "ats": "ashby", "slug": "nope"})

    def boom(ats, slug):
        raise pipeline.sources.SourceError("Board not found (404)")

    monkeypatch.setattr(pipeline.sources, "fetch", boom)
    result = client.post("/api/sync").json()
    assert result["errors"] == 1
    assert result["results"][0]["ok"] is False
    assert client.get("/api/companies").json()[0]["last_error"].startswith("Board not found")


def test_job_filters(client, monkeypatch):
    _seed_jobs(client, monkeypatch)
    assert len(client.get("/api/jobs?min_score=60").json()) == 1
    assert len(client.get("/api/jobs?q=warehouse").json()) == 1
    assert client.get("/api/jobs?q=nothing-here").json() == []

    job_id = client.get("/api/jobs").json()[1]["id"]
    client.post(f"/api/jobs/{job_id}/hide", json={"hidden": True})
    assert len(client.get("/api/jobs").json()) == 1
    assert len(client.get("/api/jobs?include_hidden=true").json()) == 2


def test_application_lifecycle(client, monkeypatch):
    _seed_jobs(client, monkeypatch)
    job_id = client.get("/api/jobs").json()[0]["id"]

    application = client.post(f"/api/jobs/{job_id}/track", json={"status": "saved"}).json()
    # Tracking twice returns the same row rather than creating a second one.
    assert client.post(f"/api/jobs/{job_id}/track", json={}).json()["id"] == application["id"]

    assert client.get("/api/jobs?only_untracked=true").json()[0]["id"] != job_id

    bad = client.patch(f"/api/applications/{application['id']}", json={"status": "invented"})
    assert bad.status_code == 400

    updated = client.patch(f"/api/applications/{application['id']}",
                           json={"status": "applied", "notes": "Referred by Sam"}).json()
    assert updated["status"] == "applied"
    assert updated["applied_at"] is not None
    assert updated["notes"] == "Referred by Sam"

    events = client.get(f"/api/applications/{application['id']}/events").json()
    assert any(event["detail"] == "saved -> applied" for event in events)

    listed = client.get("/api/applications").json()
    assert listed[0]["title"] == "Backend Engineer"


def test_generation_requires_credentials(client, monkeypatch):
    _seed_jobs(client, monkeypatch)
    job_id = client.get("/api/jobs").json()[0]["id"]
    monkeypatch.setattr(server.llm, "available", lambda: False)
    response = client.post(f"/api/jobs/{job_id}/generate/resume")
    assert response.status_code == 503
    assert "credentials" in response.json()["detail"].lower()


def test_generation_saves_and_downloads_document(client, monkeypatch):
    _seed_jobs(client, monkeypatch)
    job_id = client.get("/api/jobs").json()[0]["id"]
    monkeypatch.setattr(server.tailor, "tailored_resume", lambda profile, job: "# Alex Doe\n\nTailored.")

    document = client.post(f"/api/jobs/{job_id}/generate/resume").json()
    assert document["kind"] == "resume"
    assert "Tailored." in document["content"]

    # Generating also starts tracking the job.
    assert client.get(f"/api/jobs/{job_id}").json()["application"] is not None
    assert client.get(f"/api/jobs/{job_id}").json()["documents"][0]["kind"] == "resume"

    download = client.get(f"/api/documents/{document['id']}/download")
    assert download.status_code == 200
    assert "attachment" in download.headers["content-disposition"]
    assert ".md" in download.headers["content-disposition"]


def test_review_generation_renders_markdown(client, monkeypatch):
    _seed_jobs(client, monkeypatch)
    job_id = client.get("/api/jobs").json()[0]["id"]
    monkeypatch.setattr(server.tailor, "deep_review", lambda profile, job: {
        "score": 78, "verdict": "worth applying",
        "strengths": ["Owns billing systems"], "gaps": ["No Kafka"],
        "talking_points": ["Lead with billing"], "resume_advice": "Move billing to the top.",
    })
    document = client.post(f"/api/jobs/{job_id}/generate/review").json()
    assert "worth applying" in document["content"]
    assert "Owns billing systems" in document["content"]
    assert document["review"]["score"] == 78


def test_generate_unknown_kind_and_missing_job(client, monkeypatch):
    _seed_jobs(client, monkeypatch)
    assert client.post("/api/jobs/999999/generate/resume").status_code == 404
    job_id = client.get("/api/jobs").json()[0]["id"]
    assert client.post(f"/api/jobs/{job_id}/generate/nonsense").status_code == 422


def test_rescore_after_profile_change(client, monkeypatch):
    _seed_jobs(client, monkeypatch)
    before = client.get("/api/jobs").json()[0]["score"]

    client.put("/api/profile", json={**PROFILE, "target_titles": ["Data Scientist"],
                                     "skills": ["R", "SAS"]})
    assert client.post("/api/rescore").json()["rescored"] == 2
    after = next(j for j in client.get("/api/jobs?min_score=0").json() if j["title"] == "Backend Engineer")
    assert after["score"] < before
