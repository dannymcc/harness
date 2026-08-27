"""Reproduces #110: subprocess.TimeoutExpired escaping gh.run() as itself,
rather than as a CmdError, past the two live call sites that only catch
CmdError.

gh.run() is expected to catch subprocess.TimeoutExpired and re-raise it as
gh.CmdTimeout, a CmdError subclass — see gh.py's run(). Until that lands:

- test_gh_run_raises_cmdtimeout_not_a_raw_timeoutexpired fails with
  AttributeError (gh.CmdTimeout does not exist yet).
- the other two fail because the raw TimeoutExpired escapes past
  pipeline.py's `except CmdError` handlers at line 1587 and 2192.

All three fake the hang at the subprocess.run level (as test_gh.py's `fake`
fixture already does for the non-timeout paths), so they exercise whatever
conversion gh.run() itself does -- not a stand-in for it.
"""
import subprocess

import pytest


def test_gh_run_raises_cmdtimeout_not_a_raw_timeoutexpired(monkeypatch):
    """CmdTimeout must subclass CmdError and carry enough detail for the
    existing park/warn messages (which read `e` and `e.err`/`e.out`, or do
    an isinstance check) to stay as informative as they are today."""
    from harness import gh

    def _hangs(cmd, cwd=None, capture_output=True, text=True, timeout=600,
              env=None):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout,
                                        output="partial output\n")

    monkeypatch.setattr(subprocess, "run", _hangs)

    with pytest.raises(gh.CmdTimeout) as excinfo:
        gh.run(["gh", "issue", "create", "-R", "owner/repo"], timeout=45)

    err = excinfo.value
    assert isinstance(err, gh.CmdError)          # every `except CmdError` covers it
    assert "45" in str(err)                      # the timeout value survives
    assert "timed out" in str(err).lower() or "timeout" in str(err).lower()


def test_open_tracking_issues_survives_a_hung_create(fresh_db, may,
                                                      monkeypatch):
    """pipeline.py:1587 (_open_tracking_issues) catches CmdError only. A hung
    `gh issue create` must not crash the whole tracking-issue loop -- the
    only net above it is worker.py's broad `except Exception`, which would
    log the entire cycle as crashed and abandon the rest of that cycle's
    work. The intended behaviour, like every other CmdError here, is a warn
    and continue to the next tracking issue."""
    from harness import gh, pipeline

    def _subprocess_run(cmd, cwd=None, capture_output=True, text=True,
                        timeout=600, env=None):
        if any("First (hangs)" in a for a in cmd):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
        return subprocess.CompletedProcess(
            cmd, 0, "https://github.com/example/may/issues/99\n", "")

    monkeypatch.setattr(subprocess, "run", _subprocess_run)

    # Must not raise -- and the second, unrelated tracking issue must still
    # be attempted and filed.
    pipeline._open_tracking_issues(may, [
        {"title": "First (hangs)", "body": "b"},
        {"title": "Second", "body": "b"},
    ])

    item = fresh_db.get_item("may", "issue", 99)
    assert item is not None and item["title"] == "Second"
    assert any("First (hangs)" in e["message"]
               for e in fresh_db.recent_events(10, "may"))


def test_close_item_survives_a_hung_github_close(fresh_db, may, monkeypatch):
    """pipeline.py:2192 (close_item) catches CmdError only. A hung
    `gh issue close` must strand the item no worse than a rejected close
    does: the board still moves it to 'closed' locally (db.update_item must
    still run, so the item leaves the queues) and the operator gets the
    specific 'GitHub close failed' warning, not a crashed directive
    action."""
    from harness import gh, pipeline

    def _hangs(cmd, cwd=None, capture_output=True, text=True, timeout=600,
              env=None):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", _hangs)
    fresh_db.upsert_item("may", "issue", 40, "t", "alice", "open", "x")

    assert pipeline.close_item(may, "issue", 40)

    item = fresh_db.get_item("may", "issue", 40)
    assert item["status"] == "closed" and item["gh_state"] == "open"
    assert any("GitHub close failed" in e["message"]
               for e in fresh_db.recent_events(20, "may"))
