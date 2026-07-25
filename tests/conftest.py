import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point the app at a throwaway database for the duration of one test."""
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(config, "DOCS_DIR", tmp_path / "documents")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init()
    yield db
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
        db._local.conn = None
