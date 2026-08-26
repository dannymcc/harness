import os
import sys
import tempfile
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp(prefix="harness-tests-")
os.environ["HARNESS_DATA_DIR"] = _tmp
# Every test writes to a throwaway database, so there is nothing to protect
# with an fsync per commit — and at ~100ms each they dominate the suite's
# runtime. Set before harness.config is first imported, since it reads the
# environment at import time.
os.environ["HARNESS_DB_SYNCHRONOUS"] = "OFF"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def fresh_db(monkeypatch):
    """Each test gets an isolated data dir (and therefore SQLite DB)."""
    import importlib
    d = tempfile.mkdtemp(prefix="harness-test-")
    from harness import config
    monkeypatch.setattr(config, "DATA_DIR", Path(d))
    monkeypatch.setattr(config, "REPOS_DIR", Path(d) / "repos")
    monkeypatch.setattr(config, "DB_PATH", Path(d) / "harness.db")
    monkeypatch.setattr(config, "LOG_DIR", Path(d) / "logs")
    from harness import db
    return db


@pytest.fixture()
def may(fresh_db):
    fresh_db.create_project("may", "example/may")
    return fresh_db.get_project("may")


@pytest.fixture()
def client(fresh_db, may):
    from fastapi.testclient import TestClient
    from harness.web.app import app
    return TestClient(app)
