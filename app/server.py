"""FastAPI application: JSON API plus the single-page UI."""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, llm, pipeline, prep, sources, tailor

STATIC_DIR = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(_: FastAPI):
    config.ensure_dirs()
    db.init()
    yield


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


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")[:60] or "untitled"


def _save_document(job: dict[str, Any], kind: str, content: str) -> dict[str, Any]:
    cursor = db.execute(
        "INSERT INTO documents (job_id, kind, content, created_at) VALUES (?, ?, ?, ?)",
        (job["id"], kind, content, db.now()),
    )
    doc_id = int(cursor.lastrowid)

    filename = f"{_slugify(job['company'])}-{_slugify(job['title'])}-{kind}-{doc_id}.md"
    path = config.DOCS_DIR / filename
    path.write_text(content, encoding="utf-8")

    application = db.query_one("SELECT id FROM applications WHERE job_id = ?", (job["id"],))
    if application:
        db.add_event(application["id"], "document", f"Generated {kind.replace('_', ' ')}")

    return {"id": doc_id, "job_id": job["id"], "kind": kind, "content": content,
            "created_at": db.now(), "file": str(path)}


def _review_to_markdown(review: dict[str, Any]) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) or "- (none)"

    return (
        f"# Fit review — {review.get('verdict', 'unknown')} ({review.get('score', '?')}/100)\n\n"
        f"## Strengths\n{bullets(review.get('strengths', []))}\n\n"
        f"## Gaps\n{bullets(review.get('gaps', []))}\n\n"
        f"## Talking points\n{bullets(review.get('talking_points', []))}\n\n"
        f"## Resume advice\n{review.get('resume_advice', '')}\n"
    )


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
            "boards.greenhouse.io, job-boards.greenhouse.io, jobs.lever.co, jobs.ashbyhq.com",
        )
    return detected


# --- sync & scoring ----------------------------------------------------------


@app.post("/api/sync")
def sync() -> dict[str, Any]:
    return pipeline.sync_all()


@app.post("/api/rescore")
def rescore() -> dict[str, Any]:
    return pipeline.rescore_all()


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
        status = payload["status"]
        if status not in db.STATUSES:
            raise HTTPException(400, f"status must be one of: {', '.join(db.STATUSES)}")
        if status != row["status"]:
            db.add_event(application_id, "status", f"{row['status']} -> {status}")
        applied_at = row["applied_at"]
        if status == "applied" and not applied_at:
            applied_at = db.now()
        db.execute(
            "UPDATE applications SET status = ?, applied_at = ?, updated_at = ? WHERE id = ?",
            (status, applied_at, db.now(), application_id),
        )

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
            content = _review_to_markdown(review)
        elif kind == "interview_prep":
            content = prep.interview_prep(profile, job)
        else:
            content = prep.onboarding_plan(profile, job)
    except llm.LLMError as exc:
        raise HTTPException(503, str(exc)) from exc

    db.get_or_create_application(job_id)
    document = _save_document(job, kind, content)
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
    filename = f"{_slugify(row['company'])}-{_slugify(row['title'])}-{row['kind']}.md"
    return PlainTextResponse(
        row["content"],
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/documents/{document_id}")
def delete_document(document_id: int) -> dict[str, Any]:
    db.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    return {"deleted": document_id}


# --- UI ----------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
