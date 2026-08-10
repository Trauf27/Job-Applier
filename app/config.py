"""Runtime configuration.

Everything is resolved from environment variables with sensible defaults so the
app runs with zero setup. A `.env` file in the project root is loaded if present.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=value lines, # comments, optional quotes."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Real environment variables always win over the file.
        os.environ.setdefault(key, value)


_load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = Path(os.environ.get("JOB_APPLIER_DATA", PROJECT_ROOT / "data"))
DB_PATH = DATA_DIR / "job_applier.sqlite3"
DOCS_DIR = DATA_DIR / "documents"

HOST = os.environ.get("JOB_APPLIER_HOST", "127.0.0.1")
PORT = int(os.environ.get("JOB_APPLIER_PORT", "8765"))

# Anthropic
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Browser used to fill application forms. Playwright normally manages its own
# Chromium; point this at an existing binary when Playwright's pinned build
# isn't the one installed (it raises "Executable doesn't exist at …" when so).
BROWSER_PATH = os.environ.get("JOB_APPLIER_BROWSER_PATH", "")

# Networking for job-board fetches
HTTP_TIMEOUT = float(os.environ.get("JOB_APPLIER_HTTP_TIMEOUT", "20"))
USER_AGENT = os.environ.get(
    "JOB_APPLIER_USER_AGENT",
    "job-applier/0.1 (personal job search tool)",
)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)


def slugify(value: str) -> str:
    """Filename-safe fragment. Shared so a job's documents, its rendered PDF, and
    its screenshots all sort together in `data/documents`."""
    import re

    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")[:60] or "untitled"
