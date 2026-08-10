"""The agent's policy.

Every number here is a limit on what the agent may do without asking. The
defaults are deliberately timid: dry run on, auto-apply off, three applications
a day. Loosen them once you've read a few runs' logs and trust what it picks.
"""

from __future__ import annotations

import json
from typing import Any

from .. import db

DEFAULTS: dict[str, Any] = {
    # --- when it runs -------------------------------------------------------
    "enabled": False,            # master switch for the background scheduler
    "interval_minutes": 360,     # 6h; feeds ask that you don't poll harder
    # --- what it looks at ---------------------------------------------------
    "feeds": ["remotive"],       # open feeds to search, alongside your boards
    "sync_boards": True,         # also pull the company boards you've added
    # --- what it shortlists -------------------------------------------------
    "min_score": 70,             # local score needed to spend a model call
    "review_min_score": 65,      # Claude's own fit score needed to go further
    "skip_verdicts": ["skip"],   # fit verdicts that end a job's run
    "company_blocklist": [],     # never apply here, whatever the score
    # --- how much it may spend ----------------------------------------------
    "max_reviews_per_run": 8,
    "max_documents_per_run": 4,  # jobs fully tailored per run (2 calls each)
    "max_applications_per_run": 2,
    "max_applications_per_day": 3,
    # --- how far it goes ----------------------------------------------------
    "dry_run": True,             # plan and log, change nothing
    "auto_apply": False,         # let the browser driver fill the form
    "auto_submit": False,        # …and press submit. Requires auto_apply.
    "headless": False,           # watch it work; headless hides the browser
    # --- afterwards ---------------------------------------------------------
    "followup_days": 10,         # flag applications that have gone quiet
}

BOOL_KEYS = {"enabled", "sync_boards", "dry_run", "auto_apply", "auto_submit", "headless"}
INT_KEYS = {
    "interval_minutes", "min_score", "review_min_score", "max_reviews_per_run",
    "max_documents_per_run", "max_applications_per_run", "max_applications_per_day",
    "followup_days",
}
LIST_KEYS = {"feeds", "skip_verdicts", "company_blocklist"}

# Nothing below these floors, nothing above these ceilings — a typo in the UI
# shouldn't turn into eighty applications overnight.
BOUNDS = {
    "interval_minutes": (15, 10_080),
    "min_score": (0, 100),
    "review_min_score": (0, 100),
    "max_reviews_per_run": (0, 50),
    "max_documents_per_run": (0, 25),
    "max_applications_per_run": (0, 25),
    "max_applications_per_day": (0, 50),
    "followup_days": (1, 365),
}


def _coerce(raw: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    for key, value in (raw or {}).items():
        if key not in DEFAULTS:
            continue
        if key in BOOL_KEYS:
            merged[key] = bool(value)
        elif key in INT_KEYS:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            low, high = BOUNDS[key]
            merged[key] = max(low, min(high, number))
        elif key in LIST_KEYS:
            if isinstance(value, str):
                value = [part.strip() for part in value.splitlines()]
            merged[key] = [str(item).strip() for item in (value or []) if str(item).strip()]
        else:
            merged[key] = value

    # Submitting without filling is meaningless, and neither happens in a dry run.
    if not merged["auto_apply"]:
        merged["auto_submit"] = False
    if merged["dry_run"]:
        merged["auto_submit"] = False
    return merged


def get() -> dict[str, Any]:
    row = db.query_one("SELECT data, updated_at FROM agent_settings WHERE id = 1")
    if row is None:
        return dict(DEFAULTS, updated_at=None)
    settings = _coerce(json.loads(row["data"]))
    settings["updated_at"] = row["updated_at"]
    return settings


def save(payload: dict[str, Any]) -> dict[str, Any]:
    merged = _coerce({**get(), **(payload or {})})
    merged.pop("updated_at", None)
    db.execute(
        """
        INSERT INTO agent_settings (id, data, updated_at) VALUES (1, ?, ?)
        ON CONFLICT (id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at
        """,
        (json.dumps(merged), db.now()),
    )
    return get()


def describe(settings: dict[str, Any]) -> str:
    """One line saying how far this configuration will actually go — shown in the
    UI so the mode is never a surprise."""
    if settings["dry_run"]:
        return "Dry run: it will score, shortlist, and log what it would do. Nothing is written."
    if not settings["auto_apply"]:
        return ("It will find, review, and tailor — then stop, leaving applications at "
                "'ready' for you to submit.")
    if not settings["auto_submit"]:
        return ("It will fill each application form in a real browser and stop at the "
                "submit button for you to check.")
    return (f"It will fill and submit up to {settings['max_applications_per_day']} "
            "application(s) a day, skipping any form with unanswered required fields.")
