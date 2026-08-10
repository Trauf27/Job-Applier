"""FastAPI application: JSON API plus the single-page UI."""

from __future__ import annotations

import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import agent, config, db, documents, llm, pipeline, prep, sources, tailor
from . import apply as apply_mod

STATIC_DIR = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(_: FastAPI):
    config.ensure_dirs()
    db.init()
    # The scheduler decides for itself whether the agent is enabled, so starting
    # it unconditionally is safe and means toggling the switch in the UI takes
    # effect without a restart.
    agent.scheduler.start_in_background()
    try:
        yield
    finally:
        agent.scheduler.stop()


app = FastAPI(title="Job Applier", version="0.1.0", lifespan=lifespan)


# --- helpers -----------------------------------------------------------------


def _job_or_404(job_id: int) -> dict[str, Any]:
    job = db.row_to_dict(db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,)))
    if job is None:
        raise HTTPException(404, f"No job with id {job_id}")
    job["remote"] = bool(job["remote"])
    if job.get("score_detail"):
        job["score_detail"] = json.loads(job["score_detail"])
    return job


def _profile_or_400() -> dict[str, Any]:
    profile = db.get_profile()
    if not profile.get("name") and not profile.get("experience"):
        raise HTTPException(400, "Fill in your profile first — it's the input to everything here.")
    return profile





# --- status ------------------------------------------------------------------


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    counts = db.query_one(
        """
        SELECT (SELECT COUNT(*) FROM companies WHERE enabled = 1) AS companies,
               (SELECT COUNT(*) FROM jobs WHERE hidden = 0)       AS jobs,
               (SELECT COUNT(*) FROM applications)                AS applications
        """
    )
    profile = db.get_profile()
    return {
        "llm": llm.status(),
        "counts": dict(counts) if counts else {},
        "profile_ready": bool(profile.get("name") or profile.get("experience")),
        "statuses": db.STATUSES,
        "ats_choices": sources.ATS_CHOICES,
        "feed_choices": sources.FEED_CHOICES,
        "agent": agent.scheduler.status(),
        "autofill_available": apply_mod.browser.available(),
        "data_dir": str(config.DATA_DIR),
    }


# --- profile -----------------------------------------------------------------


@app.get("/api/profile")
def get_profile() -> dict[str, Any]:
    return db.get_profile()


@app.put("/api/profile")
def put_profile(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return db.save_profile(payload)


@app.post("/api/profile/parse-resume")
def parse_resume(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Extract a draft profile from pasted resume text. Does not save."""
    text = (payload.get("text") or "").strip()
    if len(text) < 50:
        raise HTTPException(400, "Paste at least a few lines of your resume.")
    try:
        return tailor.parse_resume(text)
    except llm.LLMError as exc:
        raise HTTPException(503, str(exc)) from exc


# --- companies ---------------------------------------------------------------


@app.get("/api/companies")
def list_companies() -> list[dict[str, Any]]:
    rows = db.query("SELECT * FROM companies ORDER BY name COLLATE NOCASE")
    return [dict(row, enabled=bool(row["enabled"])) for row in rows]


@app.post("/api/companies")
def add_company(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ats = (payload.get("ats") or "").strip().lower()
    slug = (payload.get("slug") or "").strip().strip("/")
    name = (payload.get("name") or slug).strip()
    if ats not in sources.ATS_CHOICES:
        raise HTTPException(400, f"ats must be one of: {', '.join(sources.ATS_CHOICES)}")
    if not slug:
        raise HTTPException(400, "slug is required (the company's board identifier)")

    existing = db.query_one("SELECT id FROM companies WHERE ats = ? AND slug = ?", (ats, slug))
    if existing:
        raise HTTPException(409, f"{name} ({ats}) is already tracked")

    cursor = db.execute(
        "INSERT INTO companies (name, ats, slug, enabled) VALUES (?, ?, ?, 1)",
        (name, ats, slug),
    )
    return dict(db.query_one("SELECT * FROM companies WHERE id = ?", (cursor.lastrowid,)))


@app.patch("/api/companies/{company_id}")
def update_company(company_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM companies WHERE id = ?", (company_id,))
    if row is None:
        raise HTTPException(404, "No such company")
    if "enabled" in payload:
        db.execute(
            "UPDATE companies SET enabled = ? WHERE id = ?",
            (int(bool(payload["enabled"])), company_id),
        )
    if payload.get("name"):
        db.execute("UPDATE companies SET name = ? WHERE id = ?", (payload["name"], company_id))
    return dict(db.query_one("SELECT * FROM companies WHERE id = ?", (company_id,)))


@app.delete("/api/companies/{company_id}")
def delete_company(company_id: int) -> dict[str, Any]:
    db.execute("DELETE FROM companies WHERE id = ?", (company_id,))
    return {"deleted": company_id}


@app.get("/api/companies/suggestions")
def company_suggestions() -> list[dict[str, str]]:
    from .sources.companies import SUGGESTIONS

    return SUGGESTIONS


@app.post("/api/companies/detect")
def detect_company(payload: dict[str, Any] = Body(...)) -> dict[str, str]:
    """Turn a pasted job/board URL into an {ats, slug, name} triple."""
    from .sources.companies import detect_from_url

    detected = detect_from_url(payload.get("url", ""))
    if detected is None:
        raise HTTPException(
            400,
            "Could not read a board from that URL. Supported hosts: "
            "greenhouse.io, jobs.lever.co, jobs.ashbyhq.com, workable.com, "
            "smartrecruiters.com, recruitee.com. For anything else, use the "
            "'Paste any job' box below.",
        )
    return detected


# --- sync & scoring ----------------------------------------------------------


@app.post("/api/sync")
def sync() -> dict[str, Any]:
    return pipeline.sync_all()


@app.post("/api/rescore")
def rescore() -> dict[str, Any]:
    return pipeline.rescore_all()


@app.post("/api/feeds/sync")
def sync_feeds(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    feeds = payload.get("feeds") or agent.settings.get()["feeds"]
    unknown = [f for f in feeds if f not in sources.FEED_CHOICES]
    if unknown:
        raise HTTPException(400, f"Unknown feed(s): {', '.join(unknown)}")
    return pipeline.sync_feeds(feeds)


# --- LinkedIn import ----------------------------------------------------------


@app.post("/api/linkedin/import")
def import_linkedin(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Ingest LinkedIn postings from content you supply.

    LinkedIn is never fetched — paste a job alert email, a saved search page, or
    a list of posting URLs and they're parsed here. See `sources/linkedin.py`
    for why it works this way.
    """
    text = (payload.get("text") or "").strip()
    if len(text) < 10:
        raise HTTPException(400, "Paste a LinkedIn alert email, a saved search page, or job URLs.")

    raw_jobs = sources.linkedin.parse(text)
    if not raw_jobs:
        raise HTTPException(
            400,
            "Nothing recognisable in there. Paste either a LinkedIn job-alert email, the "
            "copied text of a search results page, or one job URL per line.",
        )

    result = pipeline.ingest(raw_jobs)
    return {
        **result,
        "parsed": sources.linkedin.describe(raw_jobs),
        "message": f"Imported {result['added']} new and refreshed {result['updated']} posting(s).",
    }


@app.post("/api/jobs/import")
def import_pasted_jobs(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Import a job pasted from anywhere — Google Jobs, Rozee.pk, a company page,
    an email. Nothing is fetched; see `sources/paste.py` for the accepted shapes.
    """
    text = (payload.get("text") or "").strip()
    if len(text) < 10:
        raise HTTPException(
            400,
            "Paste a job posting. The reliable form is a few 'Label: value' lines "
            "(Title, Company, Location, URL) then the description.",
        )

    raw_jobs = sources.paste.parse(text)
    if not raw_jobs:
        raise HTTPException(
            400,
            "Couldn't find a title or company in that. Add a couple of label lines — "
            "'Title: ...' and 'Company: ...' — and try again.",
        )

    result = pipeline.ingest(raw_jobs)
    return {
        **result,
        "parsed": sources.paste.describe(raw_jobs),
        "message": f"Imported {result['added']} new and refreshed {result['updated']} posting(s).",
    }


# --- jobs --------------------------------------------------------------------


@app.get("/api/jobs")
def list_jobs(
    min_score: float = Query(0, ge=0, le=100),
    q: str = "",
    include_hidden: bool = False,
    only_untracked: bool = False,
    limit: int = Query(200, ge=1, le=1000),
) -> list[dict[str, Any]]:
    clauses = ["COALESCE(j.score, 0) >= ?"]
    params: list[Any] = [min_score]
    if not include_hidden:
        clauses.append("j.hidden = 0")
    if only_untracked:
        clauses.append("a.id IS NULL")
    if q.strip():
        clauses.append("(j.title LIKE ? OR j.company LIKE ? OR j.location LIKE ?)")
        needle = f"%{q.strip()}%"
        params += [needle, needle, needle]

    params.append(limit)
    rows = db.query(
        f"""
        SELECT j.*, a.id AS application_id, a.status AS application_status
          FROM jobs j
          LEFT JOIN applications a ON a.job_id = j.id
         WHERE {' AND '.join(clauses)}
         ORDER BY COALESCE(j.score, 0) DESC, j.first_seen DESC
         LIMIT ?
        """,
        params,
    )

    jobs = []
    for row in rows:
        job = dict(row)
        job["remote"] = bool(job["remote"])
        job["hidden"] = bool(job["hidden"])
        job["score_detail"] = json.loads(job["score_detail"]) if job["score_detail"] else None
        # The list view only needs a teaser; the detail view fetches the full text.
        job["description"] = (job["description"] or "")[:400]
        jobs.append(job)
    return jobs


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int) -> dict[str, Any]:
    job = _job_or_404(job_id)
    application = db.query_one("SELECT * FROM applications WHERE job_id = ?", (job_id,))
    job["application"] = dict(application) if application else None
    docs = db.query(
        "SELECT id, kind, created_at FROM documents WHERE job_id = ? ORDER BY created_at DESC",
        (job_id,),
    )
    job["documents"] = [dict(doc) for doc in docs]
    return job


@app.post("/api/jobs/{job_id}/hide")
def hide_job(job_id: int, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    hidden = int(bool(payload.get("hidden", True)))
    db.execute("UPDATE jobs SET hidden = ? WHERE id = ?", (hidden, job_id))
    return {"job_id": job_id, "hidden": bool(hidden)}


@app.post("/api/jobs/{job_id}/track")
def track_job(job_id: int, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    _job_or_404(job_id)
    status = payload.get("status", "saved")
    if status not in db.STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(db.STATUSES)}")
    return db.get_or_create_application(job_id, status)


# --- applications ------------------------------------------------------------


@app.get("/api/applications")
def list_applications() -> list[dict[str, Any]]:
    rows = db.query(
        """
        SELECT a.*, j.title, j.company, j.url, j.location, j.remote, j.score
          FROM applications a
          JOIN jobs j ON j.id = a.job_id
         ORDER BY a.updated_at DESC
        """
    )
    return [dict(row, remote=bool(row["remote"])) for row in rows]


@app.patch("/api/applications/{application_id}")
def update_application(application_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM applications WHERE id = ?", (application_id,))
    if row is None:
        raise HTTPException(404, "No such application")

    if "status" in payload:
        try:
            db.set_application_status(application_id, payload["status"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    if "notes" in payload:
        db.execute(
            "UPDATE applications SET notes = ?, updated_at = ? WHERE id = ?",
            (payload["notes"], db.now(), application_id),
        )

    return dict(db.query_one("SELECT * FROM applications WHERE id = ?", (application_id,)))


@app.delete("/api/applications/{application_id}")
def delete_application(application_id: int) -> dict[str, Any]:
    db.execute("DELETE FROM applications WHERE id = ?", (application_id,))
    return {"deleted": application_id}


@app.get("/api/applications/{application_id}/events")
def application_events(application_id: int) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM application_events WHERE application_id = ? ORDER BY created_at DESC",
        (application_id,),
    )
    return [dict(row) for row in rows]


# --- document generation -----------------------------------------------------

GeneratedKind = Literal["resume", "cover_letter", "review", "interview_prep", "onboarding"]


@app.post("/api/jobs/{job_id}/generate/{kind}")
def generate(job_id: int, kind: GeneratedKind) -> dict[str, Any]:
    """Generate a document for one job. Synchronous — can take 30-90 seconds."""
    job = _job_or_404(job_id)
    profile = _profile_or_400()

    try:
        if kind == "resume":
            content: str = tailor.tailored_resume(profile, job)
        elif kind == "cover_letter":
            content = tailor.cover_letter(profile, job)
        elif kind == "review":
            review = tailor.deep_review(profile, job)
            content = documents.review_to_markdown(review)
        elif kind == "interview_prep":
            content = prep.interview_prep(profile, job)
        else:
            content = prep.onboarding_plan(profile, job)
    except llm.LLMError as exc:
        raise HTTPException(503, str(exc)) from exc

    db.get_or_create_application(job_id)
    document = documents.save(job, kind, content)
    if kind == "review":
        document["review"] = review
    return document


@app.get("/api/documents/{document_id}")
def get_document(document_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
    if row is None:
        raise HTTPException(404, "No such document")
    return dict(row)


@app.get("/api/documents/{document_id}/download", response_class=PlainTextResponse)
def download_document(document_id: int) -> PlainTextResponse:
    row = db.query_one(
        """
        SELECT d.*, j.company, j.title FROM documents d
          JOIN jobs j ON j.id = d.job_id
         WHERE d.id = ?
        """,
        (document_id,),
    )
    if row is None:
        raise HTTPException(404, "No such document")
    filename = (
        f"{config.slugify(row['company'])}-{config.slugify(row['title'])}-{row['kind']}.md"
    )
    return PlainTextResponse(
        row["content"],
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/documents/{document_id}")
def delete_document(document_id: int) -> dict[str, Any]:
    db.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    return {"deleted": document_id}


# --- applying -----------------------------------------------------------------


@app.get("/api/jobs/{job_id}/attempts")
def list_attempts(job_id: int) -> list[dict[str, Any]]:
    _job_or_404(job_id)
    return apply_mod.attempts_for(job_id)


@app.post("/api/jobs/{job_id}/apply")
def apply_to_job(job_id: int, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Fill this job's application form.

    Stops at the submit button unless `submit` is explicitly true, and even then
    refuses if anything required was left unanswered. Blocking, because it drives
    a real browser — expect 20-60 seconds.
    """
    job = _job_or_404(job_id)
    profile = _profile_or_400()

    if payload.get("submit") and not payload.get("confirm"):
        raise HTTPException(
            400,
            "Submitting on your behalf needs `confirm: true` as well as `submit: true`.",
        )

    settings = agent.settings.get()
    return apply_mod.attempt(
        job, profile,
        auto_submit=bool(payload.get("submit")),
        headless=bool(payload.get("headless", settings["headless"])),
        dry_run=bool(payload.get("dry_run")),
    )


@app.get("/api/attempts/{attempt_id}/screenshot")
def attempt_screenshot(attempt_id: int) -> FileResponse:
    row = db.query_one("SELECT screenshot FROM apply_attempts WHERE id = ?", (attempt_id,))
    if row is None or not row["screenshot"]:
        raise HTTPException(404, "No screenshot for that attempt")
    path = Path(row["screenshot"])
    # Only ever serve out of the app's own documents directory.
    if not path.is_file() or config.DOCS_DIR.resolve() not in path.resolve().parents:
        raise HTTPException(404, "Screenshot file is gone")
    return FileResponse(path, media_type="image/png")


# --- the agent ----------------------------------------------------------------


@app.get("/api/agent/settings")
def get_agent_settings() -> dict[str, Any]:
    settings = agent.settings.get()
    return {
        "settings": settings,
        "summary": agent.settings.describe(settings),
        "scheduler": agent.scheduler.status(),
        "autofill_available": apply_mod.browser.available(),
        "feed_choices": sources.FEED_CHOICES,
    }


@app.put("/api/agent/settings")
def put_agent_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if payload.get("auto_submit") and not payload.get("confirm_auto_submit"):
        raise HTTPException(
            400,
            "Turning on auto-submit means applications get sent without you seeing them. "
            "Send `confirm_auto_submit: true` alongside it.",
        )
    agent.settings.save(payload)
    return get_agent_settings()


@app.post("/api/agent/run")
def run_agent(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Kick off one pass. Returns immediately; poll `/api/agent/runs` for progress."""
    if agent.runner.is_running():
        raise HTTPException(409, "A run is already in progress.")

    overrides: dict[str, Any] = {}
    if "dry_run" in payload:
        overrides["dry_run"] = bool(payload["dry_run"])

    started = threading.Event()

    def work() -> None:
        started.set()
        try:
            agent.runner.run_once(trigger="manual", overrides=overrides)
        except Exception:  # noqa: BLE001 - already recorded on the run row
            pass

    threading.Thread(target=work, name="agent-manual-run", daemon=True).start()
    started.wait(timeout=2)
    return {"started": True, "dry_run": overrides.get("dry_run", agent.settings.get()["dry_run"])}


@app.post("/api/agent/stop")
def stop_agent() -> dict[str, Any]:
    return {"stopping": agent.runner.request_stop()}


@app.get("/api/agent/runs")
def list_agent_runs(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    return {
        "runs": agent.runner.recent_runs(limit),
        "scheduler": agent.scheduler.status(),
    }


@app.get("/api/agent/runs/{run_id}")
def get_agent_run(run_id: int) -> dict[str, Any]:
    try:
        return agent.runner.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


# --- UI ----------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
