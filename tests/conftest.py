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
# The suite exercises the paths that page the operator (holds, breaker
# trips, releases) with made-up items. Inside the harness container the
# real ntfy topic is in the environment, and harness-app's own engineers
# run this suite there — so unset it before config reads it, or every test
# run sends "Held: issue#40 (may)" to the operator's phone.
for _k in ("HARNESS_NTFY_TOPIC", "HARNESS_PUBLIC_URL", "HARNESS_GITHUB_TOKEN",
           "GITHUB_TOKEN", "GH_TOKEN"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _no_outward_notifications(monkeypatch):
    """Belt and braces for the environment scrub above: nothing a test does
    may reach a real ntfy topic, whatever config was imported with."""
    from harness import config
    monkeypatch.setattr(config, "NTFY_TOPIC", "")


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    """Every test gets a throwaway home directory.

    housekeeping._prune_sdk_sessions unlinks session transcripts under
    Path.home()/".claude"/"projects", which resolves through $HOME — the one
    sweep in the harness that reaches outside DATA_DIR. fresh_db redirects
    config, and config has no say in where home is, so anything calling
    prune() would otherwise walk the real ~/.claude of whoever ran the suite.
    Nothing there matches the encoded prefixes of a per-test data directory
    today, so this is a guard rather than a fix — but it is the scoping
    itself that the tests are pinning, and a test suite is a poor place to
    find out that the scoping regressed. Autouse for the same reason
    _no_outward_notifications is: opting in is the step that gets forgotten.
    """
    # Not tmp_path/"home": test_boot.py builds its own home there to
    # exercise run.persist_claude_home, and would find it already made.
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


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
