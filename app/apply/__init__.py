"""Submitting an application.

This package is the bridge between "we generated a tailored resume" and "the
form has been filled in":

* `answers` — profile -> form values, and the rules about what never gets
  auto-answered.
* `render` — tailored Markdown -> an uploadable PDF.
* `browser` — the Playwright driver that types it all in.

`attempt()` ties them together for one job and writes the outcome to
`apply_attempts`, so every run leaves an auditable record of what was entered on
your behalf and what was deliberately left blank.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import config, db
from . import answers as answers_mod
from . import browser, render

__all__ = ["answers_mod", "browser", "render", "attempt", "prepare_files", "latest_document"]

# What a job needs before the driver is allowed near a form.
READY_DOCUMENTS = ("resume",)


def latest_document(job_id: int, kind: str) -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT * FROM documents WHERE job_id = ? AND kind = ? ORDER BY created_at DESC, id DESC LIMIT 1",
        (job_id, kind),
    )
    return db.row_to_dict(row)


def prepare_files(job: dict[str, Any]) -> dict[str, str | None]:
    """Render this job's generated documents into files a form can accept."""
    config.ensure_dirs()
    stem = f"{config.slugify(job['company'])}-{config.slugify(job['title'])}"

    resume = latest_document(job["id"], "resume")
    cover = latest_document(job["id"], "cover_letter")

    resume_path = cover_path = None
    if resume:
        resume_path = str(render.markdown_to_pdf(
            resume["content"], config.DOCS_DIR / f"{stem}-resume.pdf"
        ))
    if cover:
        cover_path = str(render.markdown_to_pdf(
            cover["content"], config.DOCS_DIR / f"{stem}-cover-letter.pdf"
        ))

    return {
        "resume_path": resume_path,
        "cover_letter_path": cover_path,
        "cover_letter_text": cover["content"] if cover else "",
    }


def missing_documents(job_id: int) -> list[str]:
    return [kind for kind in READY_DOCUMENTS if latest_document(job_id, kind) is None]


def record(job_id: int, method: str, result: browser.AttemptResult) -> dict[str, Any]:
    cursor = db.execute(
        """
        INSERT INTO apply_attempts
            (job_id, method, status, form_url, filled, unfilled, detail, screenshot, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id, method, result.status, result.form_url,
            json.dumps(result.filled), json.dumps(result.unfilled),
            result.detail, result.screenshot, db.now(),
        ),
    )
    return dict(result.as_dict(), id=int(cursor.lastrowid), job_id=job_id, method=method)


def attempt(
    job: dict[str, Any],
    profile: dict[str, Any],
    *,
    auto_submit: bool = False,
    headless: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fill (and optionally submit) the application form for one job.

    On a successful submit the tracked application moves to `applied` and the
    transition is logged, so the pipeline reflects reality without you retyping
    it. Anything short of a submit leaves the status alone.
    """
    missing = missing_documents(job["id"])
    if missing:
        result = browser.AttemptResult(
            status="skipped",
            form_url=job.get("apply_url") or job.get("url") or "",
            detail=f"No tailored {', '.join(missing)} generated yet — nothing to upload.",
        )
        return record(job["id"], "autofill", result)

    files = prepare_files(job)
    values = answers_mod.build(
        profile,
        resume_path=files["resume_path"],
        cover_letter_path=files["cover_letter_path"],
        cover_letter_text=files["cover_letter_text"],
    )
    url = job.get("apply_url") or job.get("url") or ""

    if dry_run:
        result = browser.AttemptResult(
            status="prepared",
            form_url=url,
            filled=[{"label": key, "value": answer.value[:80], "source": answer.source}
                    for key, answer in values.values.items()],
            detail=("Dry run: resume and cover letter rendered, answers resolved, "
                    "no browser opened."),
        )
        return record(job["id"], "dry-run", result)

    if not url:
        result = browser.AttemptResult(status="failed", detail="This job has no apply URL.")
        return record(job["id"], "autofill", result)

    screenshot = config.DOCS_DIR / (
        f"{config.slugify(job['company'])}-{config.slugify(job['title'])}-form.png"
    )
    result = browser.apply_to(
        url,
        values,
        auto_submit=auto_submit,
        headless=headless,
        screenshot_path=screenshot,
    )

    if result.status == "submitted":
        application = db.get_or_create_application(job["id"])
        db.set_application_status(
            application["id"], "applied",
            f"Submitted by the agent — {len(result.filled)} field(s) filled",
        )

    return record(job["id"], "autofill", result)


def attempts_for(job_id: int) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM apply_attempts WHERE job_id = ? ORDER BY created_at DESC, id DESC",
        (job_id,),
    )
    out = []
    for row in rows:
        item = dict(row)
        item["filled"] = json.loads(item["filled"] or "[]")
        item["unfilled"] = json.loads(item["unfilled"] or "[]")
        item["screenshot_name"] = Path(item["screenshot"]).name if item["screenshot"] else None
        out.append(item)
    return out
