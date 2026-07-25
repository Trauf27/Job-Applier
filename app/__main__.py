"""Entry point: `python -m app` starts the local server and opens the UI."""

from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn

from . import config, db


def main() -> None:
    parser = argparse.ArgumentParser(prog="job-applier", description="Local job search assistant")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--no-browser", action="store_true", help="Don't open a browser window")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    args = parser.parse_args()

    config.ensure_dirs()
    db.init()

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


if __name__ == "__main__":
    main()
