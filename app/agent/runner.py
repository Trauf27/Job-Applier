"""The agent loop.

One run walks a job from "posted somewhere on the internet" to "application
submitted", through six stages:

    scan  ->  shortlist  ->  review  ->  tailor  ->  apply  ->  follow up
    boards    local score    Claude's    resume +    fill the   nudge the
    + feeds   >= threshold   fit call    letter      form       quiet ones

Three things make this safe to leave running:

* **Budgets.** Every stage has a per-run cap and applying also has a per-day
  cap, counted from what's already in the database — so restarting the app or
  triggering a run by hand can't blow through the daily limit.
* **Idempotence.** A job that has been reviewed is never reviewed again, a job
  with documents is never re-tailored, and a job with a successful attempt is
  never re-applied to. Runs can overlap in scope without duplicating work.
* **A full log.** Every decision — including every skip and its reason — lands
  in `agent_actions`. If it passed on a job you liked, the log says why.

Nothing here raises into the caller. A board that 404s, a model call that fails,
a form that won't load: each is recorded against its job and the run moves on.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .. import apply as apply_mod
from .. import db, documents, llm, pipeline, tailor
from . import settings as settings_mod

# One run at a time: the stages mutate shared state and the budgets are only
# meaningful if they're counted once.
_run_lock = threading.Lock()
_stop_event = threading.Event()


class Cancelled(Exception):
    """Raised internally when a run is asked to stop between jobs."""


class _Done(Exception):
    """Internal early exit: the run finished legitimately before the last stage."""


@dataclass
class Budget:
    """Remaining allowance for one run. Decremented as work is done, never below
    zero, and checked before each unit of spend."""

    reviews: int
    documents: int
    applications: int

    def spend(self, kind: str) -> bool:
        remaining = getattr(self, kind)
        if remaining <= 0:
            return False
        setattr(self, kind, remaining - 1)
        return True


@dataclass
class RunStats:
    scanned: int = 0
    shortlisted: int = 0
    reviewed: int = 0
    passed_review: int = 0
    tailored: int = 0
    applied: int = 0
    submitted: int = 0
    followups: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


# --- run bookkeeping ----------------------------------------------------------


def _start_run(trigger: str, dry_run: bool) -> int:
    cursor = db.execute(
        "INSERT INTO agent_runs (trigger, dry_run, status, started_at) VALUES (?, ?, 'running', ?)",
        (trigger, int(dry_run), db.now()),
    )
    return int(cursor.lastrowid)


def _finish_run(run_id: int, status: str, stats: RunStats, error: str | None = None) -> None:
    db.execute(
        "UPDATE agent_runs SET status = ?, finished_at = ?, stats = ?, error = ? WHERE id = ?",
        (status, db.now(), json.dumps(stats.as_dict()), error, run_id),
    )


def _log(run_id: int, stage: str, decision: str, detail: str = "",
         job_id: int | None = None) -> None:
    db.execute(
        """
        INSERT INTO agent_actions (run_id, job_id, stage, decision, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, job_id, stage, decision, detail[:1000], db.now()),
    )


def _check_cancelled() -> None:
    if _stop_event.is_set():
        raise Cancelled


# --- stage 1: scan ------------------------------------------------------------


def _scan(run_id: int, settings: dict[str, Any], profile: dict[str, Any],
          stats: RunStats) -> None:
    if settings["sync_boards"]:
        result = pipeline.sync_all()
        if result.get("message"):
            _log(run_id, "scan", "skip", result["message"])
        else:
            stats.scanned += result["added"]
            failed = [r["company"] for r in result["results"] if not r["ok"]]
            _log(run_id, "scan", "ok",
                 f"Boards: {result['added']} new, {result['updated']} refreshed"
                 + (f"; failed: {', '.join(failed)}" if failed else ""))
            stats.errors += len(failed)

    if settings["feeds"]:
        result = pipeline.sync_feeds(settings["feeds"], profile)
        stats.scanned += result["added"]
        for item in result["results"]:
            _log(run_id, "scan", "ok" if item["ok"] else "error",
                 f"{item['feed']}: " + (
                     f"{item['added']} new, {item['updated']} refreshed"
                     if item["ok"] else item["error"]))
        stats.errors += result["errors"]


# --- stage 2: shortlist -------------------------------------------------------

# A job the agent has already vetted but not finished — because a budget ran out
# mid-way, or a call failed — must be picked up where it was left, not re-reviewed
# from scratch and not silently dropped. With small per-run budgets (the defaults
# are 4 resumes and 2 applications) most work is finished on a *later* run, so
# these two queries are what make the daily caps mean anything over time.
_VETTED = """
    EXISTS (SELECT 1 FROM agent_actions x
             WHERE x.job_id = j.id AND x.stage = 'review' AND x.decision = 'ok')
"""
_HAS_RESUME = """
    EXISTS (SELECT 1 FROM documents d WHERE d.job_id = j.id AND d.kind = 'resume')
"""


def _rows_to_jobs(rows, settings: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    blocked = {name.lower() for name in settings["company_blocklist"]}
    jobs = []
    for row in rows:
        job = dict(row)
        if (job["company"] or "").lower() in blocked:
            continue
        job["remote"] = bool(job["remote"])
        if job.get("score_detail"):
            job["score_detail"] = json.loads(job["score_detail"])
        jobs.append(job)
        if len(jobs) >= limit:
            break
    return jobs


def _shortlist(settings: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Scored jobs worth spending a model call on.

    Excludes anything already reviewed by a previous run, anything tracked past
    'saved' (you've taken it over), and anything hidden.
    """
    rows = db.query(
        f"""
        SELECT j.*, a.id AS application_id, a.status AS application_status
          FROM jobs j
          LEFT JOIN applications a ON a.job_id = j.id
         WHERE j.hidden = 0
           AND COALESCE(j.score, 0) >= ?
           AND (a.id IS NULL OR a.status = 'saved')
           AND NOT EXISTS (
                 SELECT 1 FROM agent_actions x
                  WHERE x.job_id = j.id AND x.stage = 'review'
                    -- a failed model call must not blacklist the job for good
                    AND x.decision <> 'error'
               )
           AND NOT {_HAS_RESUME}
         ORDER BY COALESCE(j.score, 0) DESC, j.first_seen DESC
         LIMIT ?
        """,
        (settings["min_score"], limit * 3),  # over-fetch: the blocklist thins this
    )
    return _rows_to_jobs(rows, settings, limit)


def _awaiting_documents(settings: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Passed review on an earlier run, but never got its documents written."""
    rows = db.query(
        f"""
        SELECT j.*, a.id AS application_id, a.status AS application_status
          FROM jobs j
          LEFT JOIN applications a ON a.job_id = j.id
         WHERE j.hidden = 0
           AND {_VETTED}
           AND NOT {_HAS_RESUME}
           AND (a.id IS NULL OR a.status IN ('saved', 'ready'))
         ORDER BY COALESCE(j.score, 0) DESC, j.first_seen DESC
         LIMIT ?
        """,
        (limit * 3,),
    )
    return _rows_to_jobs(rows, settings, limit)


def _awaiting_application(settings: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Tailored and ready, but the application never went out."""
    rows = db.query(
        f"""
        SELECT j.*, a.id AS application_id, a.status AS application_status
          FROM jobs j
          JOIN applications a ON a.job_id = j.id
         WHERE j.hidden = 0
           AND a.status = 'ready'
           AND {_VETTED}
           AND {_HAS_RESUME}
           AND NOT EXISTS (
                 SELECT 1 FROM apply_attempts t
                  WHERE t.job_id = j.id AND t.status = 'submitted'
               )
         ORDER BY COALESCE(j.score, 0) DESC, a.updated_at
         LIMIT ?
        """,
        (limit * 3,),
    )
    return _rows_to_jobs(rows, settings, limit)


# --- stage 3: review ----------------------------------------------------------


def _review(run_id: int, job: dict[str, Any], profile: dict[str, Any],
            settings: dict[str, Any], stats: RunStats) -> bool:
    """Second-pass fit check. Returns True when the job earns documents."""
    try:
        review = tailor.deep_review(profile, job)
    except llm.LLMError as exc:
        stats.errors += 1
        _log(run_id, "review", "error", str(exc), job["id"])
        return False

    stats.reviewed += 1
    documents.save(job, "review", documents.review_to_markdown(review))

    score = int(review.get("score") or 0)
    verdict = str(review.get("verdict") or "").lower()
    gaps = "; ".join(review.get("gaps", [])[:3])

    if verdict in {v.lower() for v in settings["skip_verdicts"]}:
        _log(run_id, "review", "skip",
             f"Claude's verdict: {verdict} ({score}/100). {gaps}", job["id"])
        return False
    if score < settings["review_min_score"]:
        _log(run_id, "review", "skip",
             f"Fit {score}/100 is under the {settings['review_min_score']} threshold. {gaps}",
             job["id"])
        return False

    stats.passed_review += 1
    _log(run_id, "review", "ok", f"{verdict} — fit {score}/100", job["id"])
    return True


# --- stage 4: tailor ----------------------------------------------------------


def _tailor(run_id: int, job: dict[str, Any], profile: dict[str, Any],
            settings: dict[str, Any], stats: RunStats) -> bool:
    application = db.get_or_create_application(job["id"])
    try:
        if apply_mod.latest_document(job["id"], "resume") is None:
            documents.save(job, "resume", tailor.tailored_resume(profile, job))
        if apply_mod.latest_document(job["id"], "cover_letter") is None:
            documents.save(job, "cover_letter", tailor.cover_letter(profile, job))
    except llm.LLMError as exc:
        stats.errors += 1
        _log(run_id, "tailor", "error", str(exc), job["id"])
        return False

    stats.tailored += 1
    db.set_application_status(application["id"], "ready", "Tailored by the agent")
    _log(run_id, "tailor", "ok", "Resume and cover letter ready", job["id"])
    return True


# --- stage 5: apply -----------------------------------------------------------


def _applications_today() -> int:
    """How many applications went out today, counted from the database rather
    than from anything held in memory — so restarting the app, or triggering an
    extra run by hand, can't reset the daily cap.

    Both sources count, de-duplicated by job: applications you marked applied
    yourself, and forms the agent submitted. The cap is about how much lands in
    front of employers today, not about who pressed the button.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    row = db.query_one(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT job_id FROM applications    WHERE applied_at >= ?
            UNION
            SELECT job_id FROM apply_attempts  WHERE status = 'submitted' AND created_at >= ?
        )
        """,
        (today, today),
    )
    return int(row["n"]) if row else 0


def _already_submitted(job_id: int) -> bool:
    row = db.query_one(
        "SELECT 1 FROM apply_attempts WHERE job_id = ? AND status = 'submitted' LIMIT 1",
        (job_id,),
    )
    return row is not None


def _apply(run_id: int, job: dict[str, Any], profile: dict[str, Any],
           settings: dict[str, Any], stats: RunStats) -> None:
    if _already_submitted(job["id"]):
        _log(run_id, "apply", "skip", "Already submitted in an earlier run", job["id"])
        return

    remaining_today = settings["max_applications_per_day"] - _applications_today()
    if remaining_today <= 0:
        _log(run_id, "apply", "skip",
             f"Daily cap of {settings['max_applications_per_day']} reached — "
             "left at 'ready' for tomorrow", job["id"])
        return

    result = apply_mod.attempt(
        job, profile,
        auto_submit=settings["auto_submit"],
        headless=settings["headless"],
    )
    stats.applied += 1
    if result["status"] == "submitted":
        stats.submitted += 1
        _log(run_id, "apply", "ok", result["detail"], job["id"])
    elif result["status"] in ("needs_review", "prepared"):
        _log(run_id, "apply", "needs_review", result["detail"], job["id"])
    else:
        stats.errors += 1
        _log(run_id, "apply", result["status"], result["detail"], job["id"])


# --- stage 6: follow up -------------------------------------------------------


def _followups(run_id: int, settings: dict[str, Any], stats: RunStats) -> None:
    """Flag applications that have gone quiet, once each."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=settings["followup_days"])
    ).isoformat(timespec="seconds")

    rows = db.query(
        """
        SELECT a.id, a.applied_at, j.title, j.company
          FROM applications a
          JOIN jobs j ON j.id = a.job_id
         WHERE a.status = 'applied'
           AND a.applied_at IS NOT NULL
           AND a.applied_at < ?
           AND NOT EXISTS (
                 SELECT 1 FROM application_events e
                  WHERE e.application_id = a.id AND e.kind = 'followup_due'
               )
        """,
        (cutoff,),
    )
    for row in rows:
        db.add_event(
            row["id"], "followup_due",
            f"No movement in {settings['followup_days']} days — time to nudge them.",
        )
        stats.followups += 1
        _log(run_id, "followup", "ok", f"{row['title']} at {row['company']} has gone quiet")


# --- moving one job along -----------------------------------------------------


def _advance_to_documents(run_id: int, job: dict[str, Any], profile: dict[str, Any],
                          settings: dict[str, Any], stats: RunStats,
                          budget: Budget) -> bool:
    """Tailor this job, budget permitting. A job deferred here is picked up by
    `_awaiting_documents` on the next run rather than being forgotten."""
    if not budget.spend("documents"):
        _log(run_id, "tailor", "skip",
             "Run's tailoring budget spent — first in line next run", job["id"])
        return False
    return _tailor(run_id, job, profile, settings, stats)


def _advance_to_application(run_id: int, job: dict[str, Any], profile: dict[str, Any],
                            settings: dict[str, Any], stats: RunStats,
                            budget: Budget) -> None:
    if not settings["auto_apply"]:
        _log(run_id, "apply", "skip",
             "auto_apply is off — left at 'ready' for you to submit", job["id"])
        return
    if not budget.spend("applications"):
        _log(run_id, "apply", "skip",
             "Run's application budget spent — first in line next run", job["id"])
        return
    _check_cancelled()
    _apply(run_id, job, profile, settings, stats)


# --- the run ------------------------------------------------------------------


def _plan_only(run_id: int, settings: dict[str, Any], stats: RunStats) -> None:
    """Dry run: shortlist and report, spend nothing."""
    candidates = _shortlist(settings, settings["max_reviews_per_run"])
    stats.shortlisted = len(candidates)
    for job in candidates:
        _log(run_id, "plan", "planned",
             f"Would review, tailor, and "
             f"{'apply to' if settings['auto_apply'] else 'queue'} "
             f"{job['title']} at {job['company']} (local score "
             f"{round(job['score'] or 0)})",
             job["id"])
    if not candidates:
        stats.notes.append(
            f"Nothing scored at or above {settings['min_score']}. Lower the threshold, "
            "add companies or feeds, or widen your target titles."
        )


def run_once(
    trigger: str = "manual",
    *,
    overrides: dict[str, Any] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Execute one full pass. Returns the finished run row with its actions."""
    if not _run_lock.acquire(blocking=False):
        raise RuntimeError("A run is already in progress.")

    _stop_event.clear()
    settings = {**settings_mod.get(), **(overrides or {})}
    stats = RunStats()
    run_id = _start_run(trigger, bool(settings["dry_run"]))
    status, error = "ok", None

    def say(message: str) -> None:
        if progress:
            progress(message)

    try:
        profile = db.get_profile()
        if not profile.get("name") and not profile.get("experience"):
            stats.notes.append("Profile is empty — fill it in before the agent can judge fit.")
            _log(run_id, "scan", "skip", "No profile")
            raise _Done

        say("Scanning boards and feeds…")
        _scan(run_id, settings, profile, stats)
        _check_cancelled()

        if settings["dry_run"]:
            say("Planning (dry run)…")
            _plan_only(run_id, settings, stats)
            _followups(run_id, settings, stats)
            raise _Done

        if not llm.available():
            # Without a key the agent is still useful: it finds and ranks. It
            # just can't judge fit or write anything, so it says so and stops.
            candidates = _shortlist(settings, settings["max_reviews_per_run"])
            stats.shortlisted = len(candidates)
            for job in candidates:
                db.get_or_create_application(job["id"])
                _log(run_id, "shortlist", "ok",
                     "Saved for you — no API key, so no review or tailoring", job["id"])
            stats.notes.append(
                "No Anthropic credentials, so fit review and tailoring were skipped. "
                "Set ANTHROPIC_API_KEY to let the agent finish the job."
            )
            _followups(run_id, settings, stats)
            raise _Done

        budget = Budget(
            reviews=settings["max_reviews_per_run"],
            documents=settings["max_documents_per_run"],
            applications=settings["max_applications_per_run"],
        )

        # Oldest business first: finish what earlier runs started before
        # spending anything on jobs nobody has looked at yet.
        say("Picking up where the last run stopped…")
        for job in _awaiting_application(settings, budget.applications):
            _check_cancelled()
            _advance_to_application(run_id, job, profile, settings, stats, budget)

        for job in _awaiting_documents(settings, budget.documents):
            _check_cancelled()
            say(f"Tailoring {job['title']} at {job['company']}")
            if not _advance_to_documents(run_id, job, profile, settings, stats, budget):
                continue
            _advance_to_application(run_id, job, profile, settings, stats, budget)

        candidates = _shortlist(settings, budget.reviews)
        stats.shortlisted = len(candidates)
        if not candidates:
            stats.notes.append(
                f"Nothing new scored at or above {settings['min_score']}."
            )

        for index, job in enumerate(candidates, start=1):
            _check_cancelled()
            say(f"[{index}/{len(candidates)}] {job['title']} at {job['company']}")

            if not budget.spend("reviews"):
                break
            if not _review(run_id, job, profile, settings, stats):
                continue
            _check_cancelled()
            if not _advance_to_documents(run_id, job, profile, settings, stats, budget):
                continue
            _advance_to_application(run_id, job, profile, settings, stats, budget)

        say("Checking for stale applications…")
        _followups(run_id, settings, stats)

    except _Done:
        pass
    except Cancelled:
        status = "stopped"
        stats.notes.append("Stopped partway through — everything finished so far was kept.")
    except Exception as exc:  # noqa: BLE001 - a crashed run must still close cleanly
        status, error = "error", f"{type(exc).__name__}: {exc}"
        stats.errors += 1
    finally:
        _finish_run(run_id, status, stats, error)
        _stop_event.clear()
        _run_lock.release()

    return get_run(run_id)


def request_stop() -> bool:
    """Ask the running pass to stop at the next job boundary."""
    if is_running():
        _stop_event.set()
        return True
    return False


def is_running() -> bool:
    if _run_lock.acquire(blocking=False):
        _run_lock.release()
        return False
    return True


# --- reading runs back --------------------------------------------------------


def get_run(run_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
    if row is None:
        raise KeyError(f"No run with id {run_id}")
    run = dict(row)
    run["dry_run"] = bool(run["dry_run"])
    run["stats"] = json.loads(run["stats"] or "{}")
    run["actions"] = [
        dict(action) for action in db.query(
            """
            SELECT x.*, j.title, j.company, j.url
              FROM agent_actions x
              LEFT JOIN jobs j ON j.id = x.job_id
             WHERE x.run_id = ?
             ORDER BY x.id
            """,
            (run_id,),
        )
    ]
    return run


def recent_runs(limit: int = 20) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM agent_runs ORDER BY id DESC LIMIT ?", (limit,)
    )
    runs = []
    for row in rows:
        run = dict(row)
        run["dry_run"] = bool(run["dry_run"])
        run["stats"] = json.loads(run["stats"] or "{}")
        runs.append(run)
    return runs
