"""Entry point.

`python -m app` starts the local server and opens the UI. `python -m app agent`
runs the agent loop once and prints what it did — that's the form to put in cron
or a launchd job if you'd rather not leave the server running.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser

from . import config, db


def _run_agent(args: argparse.Namespace) -> int:
    from . import agent

    overrides: dict[str, object] = {}
    if args.dry_run:
        overrides["dry_run"] = True
    if args.live:
        overrides["dry_run"] = False
    if args.apply:
        overrides["dry_run"] = False
        overrides["auto_apply"] = True

    settings = {**agent.settings.get(), **overrides}
    print(agent.settings.describe(settings), file=sys.stderr)

    run = agent.runner.run_once(
        trigger="cli",
        overrides=overrides,
        progress=lambda message: print(f"  {message}", file=sys.stderr),
    )

    if args.json:
        print(json.dumps(run, indent=2))
        return 0 if run["status"] in ("ok", "stopped") else 1

    stats = run["stats"]
    print(f"\nRun {run['id']} — {run['status']}")
    for key in ("scanned", "shortlisted", "reviewed", "tailored", "applied",
                "submitted", "followups", "errors"):
        if stats.get(key):
            print(f"  {key:<12} {stats[key]}")
    for note in stats.get("notes", []):
        print(f"  note: {note}")
    for action in run["actions"]:
        target = f" — {action['title']} at {action['company']}" if action.get("title") else ""
        print(f"  [{action['stage']}/{action['decision']}]{target}: {action['detail']}")
    if run.get("error"):
        print(f"  error: {run['error']}", file=sys.stderr)

    return 0 if run["status"] in ("ok", "stopped") else 1


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    url = f"http://{args.host}:{args.port}/"
    print(f"Job Applier — data in {config.DATA_DIR}")
    print(f"Open {url}")

    if not args.no_browser and not args.reload:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "app.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="job-applier", description="Local job search assistant")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--no-browser", action="store_true", help="Don't open a browser window")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")

    subparsers = parser.add_subparsers(dest="command")
    agent_parser = subparsers.add_parser(
        "agent",
        help="Run one pass of the agent loop and exit (for cron)",
        description="Scan, shortlist, review, tailor, and optionally apply — once.",
    )
    agent_parser.add_argument("--dry-run", action="store_true",
                              help="Plan only: no model calls, no documents, no forms")
    agent_parser.add_argument("--live", action="store_true",
                              help="Override the saved dry_run setting and do the work")
    agent_parser.add_argument("--apply", action="store_true",
                              help="Also fill application forms (implies --live)")
    agent_parser.add_argument("--json", action="store_true", help="Print the run as JSON")

    args = parser.parse_args()

    config.ensure_dirs()
    db.init()

    if args.command == "agent":
        raise SystemExit(_run_agent(args))
    raise SystemExit(_serve(args))


if __name__ == "__main__":
    main()
