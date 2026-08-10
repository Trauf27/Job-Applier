"""Background scheduling.

A daemon thread wakes once a minute, checks whether `interval_minutes` has
elapsed since the last run, and if so starts one. The interval is measured from
the last run's *start time as recorded in the database*, so restarting the app
doesn't reset the clock and can't be used — accidentally or otherwise — to run
the agent more often than its settings allow.

Runs happen on their own thread because the stages block on network and model
calls; the web server stays responsive throughout.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from .. import db
from . import runner
from . import settings as settings_mod

log = logging.getLogger("job_applier.agent")

TICK_SECONDS = 60

_thread: threading.Thread | None = None
_wake = threading.Event()
_shutdown = threading.Event()


def last_run_started_at() -> datetime | None:
    row = db.query_one("SELECT started_at FROM agent_runs ORDER BY id DESC LIMIT 1")
    if row is None or not row["started_at"]:
        return None
    try:
        stamp = datetime.fromisoformat(row["started_at"])
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def next_run_at() -> datetime | None:
    """When the scheduler will fire next, or None when it's switched off."""
    settings = settings_mod.get()
    if not settings["enabled"]:
        return None
    last = last_run_started_at()
    if last is None:
        return datetime.now(timezone.utc)
    return last + timedelta(minutes=settings["interval_minutes"])


def due() -> bool:
    scheduled = next_run_at()
    return scheduled is not None and datetime.now(timezone.utc) >= scheduled


def start_in_background() -> None:
    """Idempotent: calling this twice leaves one scheduler thread running."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _shutdown.clear()
    _thread = threading.Thread(target=_loop, name="job-applier-agent", daemon=True)
    _thread.start()


def stop() -> None:
    _shutdown.set()
    _wake.set()


def _loop() -> None:
    while not _shutdown.is_set():
        try:
            if due() and not runner.is_running():
                log.info("Agent run starting (scheduled)")
                result = runner.run_once(trigger="schedule")
                log.info("Agent run %s finished: %s", result["id"], result["stats"])
        except Exception:  # noqa: BLE001 - the scheduler must outlive a bad run
            log.exception("Scheduled agent run failed")
        _wake.wait(TICK_SECONDS)
        _wake.clear()


def status() -> dict:
    settings = settings_mod.get()
    scheduled = next_run_at()
    return {
        "enabled": settings["enabled"],
        "running": runner.is_running(),
        "interval_minutes": settings["interval_minutes"],
        "thread_alive": bool(_thread and _thread.is_alive()),
        "next_run_at": scheduled.isoformat(timespec="seconds") if scheduled else None,
        "last_run_at": (
            last_run_started_at().isoformat(timespec="seconds")
            if last_run_started_at() else None
        ),
    }
