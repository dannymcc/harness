"""Cover housekeeping.py, which owns every irreversible operation here.

Every DELETE, unlink and rmtree in the codebase lives in this one module
(issue #98), so the tests below are less about features than about limits:
each sweep must take everything outside its window and nothing inside it.
The negative cases matter more than the positive ones, and _prune_sdk_sessions
most of all -- it is the only thing in the harness that reaches outside the
project's own data directory into the operator's home.

Nothing here may touch the real ~/.claude, the real LOG_DIR or the operator's
ntfy topic: fresh_db redirects DATA_DIR/REPOS_DIR/DB_PATH/LOG_DIR, and
conftest's autouse fixtures scrub the topic and redirect $HOME.
"""
import os
import time

import pytest


def _age(path, days):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def _age_hours(path, hours):
    old = time.time() - hours * 3600
    os.utime(path, (old, old))


# --- worktree sweep ---------------------------------------------------------
#
# _prune_worktrees (issue #96) walked DATA_DIR/worktrees/<project>/ and
# removed any directory idle past WORKTREE_KEEP_DAYS on mtime alone, with no
# regard for whether an item was still going to resume into it. An item held
# awaiting an operator or Harry answer sits idle for exactly that reason --
# past three days the pruner deleted its worktree out from under it, and the
# next resume started the engineer from scratch. This is the same class of
# loss as #82, arriving by a different route.

@pytest.fixture()
def old_worktree(fresh_db, may):
    """A worktree directory named after a branch, aged past the keep window."""
    from harness import repo

    def make(branch):
        wt = repo.worktrees_dir(may) / branch.replace("/", "-")
        wt.mkdir(parents=True)
        _age(wt, 4)
        return wt

    return make


def test_a_held_items_worktree_survives_the_sweep(fresh_db, may, old_worktree):
    """An item that is not in a terminal state keeps its worktree, however
    stale its mtime -- it may be resumed into at any time."""
    from harness import housekeeping

    branch = "harness/issue-1"
    wt = old_worktree(branch)
    fresh_db.upsert_item("may", "issue", 1, "held issue", "someone",
                          "open", fresh_db.now())
    fresh_db.update_item("may", "issue", 1, status="held", branch=branch)

    housekeeping.prune()

    assert wt.exists(), (
        "prune() deleted the worktree of a held item; a resume would have "
        "started the engineer from scratch (see issue #96)")


def test_an_abandoned_worktree_is_still_pruned(fresh_db, may, old_worktree):
    """A worktree with no corresponding live item keeps being swept after
    WORKTREE_KEEP_DAYS, so the guard doesn't turn the sweep into a no-op."""
    from harness import housekeeping

    wt = old_worktree("harness/issue-2")

    housekeeping.prune()

    assert not wt.exists()


def test_the_skip_is_visible_in_the_summary(fresh_db, may, old_worktree):
    """A worktree the sweep passes over is counted, not silently skipped --
    prune()'s summary is what run() puts on the event log."""
    from harness import housekeeping

    branch = "harness/issue-4"
    old_worktree(branch)
    old_worktree("harness/issue-5")
    fresh_db.upsert_item("may", "issue", 4, "held issue", "someone",
                         "open", fresh_db.now())
    fresh_db.update_item("may", "issue", 4, status="held", branch=branch)

    summary = housekeeping.prune()

    assert "1 stale worktrees removed" in summary
    assert "1 stale worktrees kept (in play)" in summary


def test_the_worktree_prune_cannot_block_forever(fresh_db, may, old_worktree,
                                                 monkeypatch):
    """`git worktree prune` used to run with no timeout at all, so a wedged
    git in one clone held the hourly sweep open indefinitely (#110). It goes
    through gh.run now: bounded, and a hang comes back as CmdTimeout for the
    sweep to note and carry on past."""
    import subprocess
    from harness import config, housekeeping

    (config.REPOS_DIR / "may" / ".git").mkdir(parents=True)
    wt = old_worktree("harness/issue-6")
    seen = {}

    def _wedged(cmd, cwd=None, capture_output=True, text=True, timeout=600,
                env=None):
        seen["timeout"] = timeout
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", _wedged)

    summary = housekeeping.prune()      # must not raise, must not hang

    assert seen["timeout"] and seen["timeout"] <= 300
    assert not wt.exists()              # the rest of the sweep still ran
    assert "1 stale worktrees removed" in summary
    assert any("worktree prune did not finish" in e["message"]
               for e in fresh_db.recent_events(20, "may"))


def test_a_released_items_worktree_is_pruned_too(fresh_db, may, old_worktree):
    """Terminal-status items (released/closed/rejected) are done for good;
    their worktrees are not protected from the sweep."""
    from harness import housekeeping

    branch = "harness/issue-3"
    wt = old_worktree(branch)
    fresh_db.upsert_item("may", "issue", 3, "released issue", "someone",
                          "closed", fresh_db.now())
    fresh_db.update_item("may", "issue", 3, status="released", branch=branch)

    housekeeping.prune()

    assert not wt.exists()


# --- orphaned runs ----------------------------------------------------------

def _backdate_run(fresh_db, run_id, hours):
    from datetime import datetime, timedelta, timezone
    when = (datetime.now(timezone.utc) - timedelta(hours=hours)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    with fresh_db.conn() as c:
        c.execute("UPDATE runs SET started_at = ? WHERE id = ?",
                  (when, run_id))


def test_housekeeping_orphans_count_towards_the_breaker(fresh_db, may):
    """The counterpart to test_restart_orphans_do_not_trip_the_breaker.

    A restart orphan is proof the process died and says nothing about the
    item, so the breaker skips it. A run housekeeping sweeps up produced
    nothing for hours with the process still up -- it may be an agent
    hanging on this very item, which is what the breaker is for. That
    asymmetry is deliberate (issue #97); this test pins it so the two
    summaries cannot quietly become one again.
    """
    from harness import db, housekeeping

    fresh_db.upsert_item("may", "issue", 11, "fine", "a", "open", "x")
    rid = fresh_db.start_run("may", "ic", "issue#11", "fix", "m", "Malcolm")
    _backdate_run(fresh_db, rid, housekeeping.ORPHAN_RUN_HOURS + 1)

    housekeeping.prune()

    run = fresh_db.get_run(rid)
    assert run["finished_at"] and run["ok"] == 0
    assert run["summary"] == db.HOUSEKEEPING_ORPHAN_SUMMARY, (
        "the sweep must write the shared constant, not a second literal")
    assert fresh_db.consecutive_failures("may", "issue#11") == 1, (
        "a housekeeping orphan is counted by the breaker on purpose; if it "
        "is being skipped, the exemption meant for restart orphans has "
        "leaked across")


def test_a_run_still_inside_the_window_is_left_alone(fresh_db, may):
    """ORPHAN_RUN_HOURS is the whole point of the sweep: a session that
    started a moment ago is working, not orphaned."""
    from harness import housekeeping

    fresh_db.upsert_item("may", "issue", 12, "in flight", "a", "open", "x")
    rid = fresh_db.start_run("may", "ic", "issue#12", "fix", "m", "Malcolm")

    housekeeping.prune()

    run = fresh_db.get_run(rid)
    assert run["finished_at"] is None, (
        "the sweep closed a run that had only just started")
    assert run["ok"] is None


def test_a_finished_run_is_not_reopened_or_relabelled(fresh_db, may):
    """Only `finished_at IS NULL` rows are candidates. A run that recorded
    its own result keeps it however old it is -- rewriting the summary of a
    successful run would corrupt the breaker's view of the item."""
    from harness import housekeeping

    fresh_db.upsert_item("may", "issue", 13, "done", "a", "open", "x")
    rid = fresh_db.start_run("may", "ic", "issue#13", "fix", "m", "Malcolm")
    fresh_db.finish_run(rid, True, 0.4, 3, "raised the PR")
    _backdate_run(fresh_db, rid, housekeeping.ORPHAN_RUN_HOURS + 5)

    housekeeping.prune()

    run = fresh_db.get_run(rid)
    assert run["ok"] == 1 and run["summary"] == "raised the PR"
    assert fresh_db.consecutive_failures("may", "issue#13") == 0


# --- retention windows ------------------------------------------------------
#
# The three table pruners all work the same way: order by id, look up the
# id sitting one past the keep count, delete everything at or below it. The
# tests fill relative to the real constants rather than monkeypatching them
# down to a miniature, so the windows are exercised at the size they actually
# run at -- but that does mean they pin the boundary logic (which end of the
# ordering survives, and whether the row at the offset goes with the old or
# the new) rather than the value of EVENT_KEEP/RUN_KEEP/REPORT_KEEP, which
# are tunables and deliberately left free to change.

def _bulk_events(fresh_db, n):
    ts = fresh_db.now()
    with fresh_db.conn() as c:
        # Start from empty: creating the project logs an event of its own,
        # and these tests count to the row.
        c.execute("DELETE FROM events")
        c.executemany(
            "INSERT INTO events (project, ts, level, message) "
            "VALUES ('may', ?, 'info', ?)",
            [(ts, f"event-{i}") for i in range(n)])


def _remaining_events(fresh_db):
    with fresh_db.conn() as c:
        return [r["message"] for r in
                c.execute("SELECT message FROM events ORDER BY id").fetchall()]


def test_events_at_the_keep_count_are_all_kept(fresh_db, may):
    """A table sitting exactly on the window has nothing past it, and the
    OFFSET lookup finds no row -- the sweep must then delete nothing at all
    rather than falling through to an unbounded DELETE."""
    from harness import housekeeping

    _bulk_events(fresh_db, housekeeping.EVENT_KEEP)
    with fresh_db.conn() as c:
        assert housekeeping._prune_events(c) == 0
    assert len(_remaining_events(fresh_db)) == housekeeping.EVENT_KEEP


def test_events_past_the_keep_count_are_folded_oldest_first(fresh_db, may):
    """Past the window the oldest rows go and the newest stay -- the event
    log is read newest-first, so losing the wrong end would empty the
    dashboard while leaving the table just as large."""
    from harness import housekeeping

    extra = 5
    _bulk_events(fresh_db, housekeeping.EVENT_KEEP + extra)
    with fresh_db.conn() as c:
        assert housekeeping._prune_events(c) == extra

    left = _remaining_events(fresh_db)
    assert len(left) == housekeeping.EVENT_KEEP
    assert left[0] == f"event-{extra}", "the newest events must be the survivors"
    assert left[-1] == f"event-{housekeeping.EVENT_KEEP + extra - 1}"


def _bulk_runs(fresh_db, projects, cost=1.0):
    ts = fresh_db.now()
    with fresh_db.conn() as c:
        c.executemany(
            "INSERT INTO runs (project, role, item_key, task, model, "
            "cost_usd, started_at, finished_at) "
            "VALUES (?, 'ic', '', 'fix', 'm', ?, ?, ?)",
            [(p, cost, ts, ts) for p in projects])


def test_runs_at_the_keep_count_are_all_kept(fresh_db, may):
    """As above for runs, and nothing is archived: an aggregate written on a
    sweep that folded no rows would double-count the spend."""
    from harness import housekeeping

    _bulk_runs(fresh_db, ["may"] * housekeeping.RUN_KEEP)
    with fresh_db.conn() as c:
        assert housekeeping._prune_runs(c) == 0
    assert fresh_db.get_setting("archived_cost.may") == ""


def test_pruned_runs_fold_their_cost_into_a_per_project_total(fresh_db, may):
    """The rows go, the spend does not -- total_cost reads the archived
    aggregate, so a pruned run's cost has to land under its own project."""
    from harness import housekeeping

    fresh_db.create_project("other", "example/other")
    # Oldest first, alternating: the five rows past the window are three
    # "may" and two "other", so a GROUP BY that has slipped shows up as a
    # total on the wrong key.
    projects = [("may" if i % 2 == 0 else "other") for i in range(5)]
    projects += ["may"] * housekeeping.RUN_KEEP
    _bulk_runs(fresh_db, projects)

    with fresh_db.conn() as c:
        assert housekeeping._prune_runs(c) == 5

    assert float(fresh_db.get_setting("archived_cost.may")) == 3.0
    assert float(fresh_db.get_setting("archived_cost.other")) == 2.0
    with fresh_db.conn() as c:
        assert c.execute("SELECT COUNT(*) n FROM runs").fetchone()["n"] \
            == housekeeping.RUN_KEEP


def test_archived_cost_accumulates_across_sweeps(fresh_db, may):
    """Each sweep adds to the running total rather than replacing it."""
    from harness import housekeeping

    fresh_db.set_setting("archived_cost.may", "2.5")
    _bulk_runs(fresh_db, ["may"] * (housekeeping.RUN_KEEP + 2))

    with fresh_db.conn() as c:
        housekeeping._prune_runs(c)

    assert float(fresh_db.get_setting("archived_cost.may")) == 4.5


def test_reports_are_kept_per_scope_and_project(fresh_db, may):
    """REPORT_KEEP is a window per (scope, project), not a global cap: a
    scope that has only ever written twice must come through untouched."""
    from harness import housekeeping

    for i in range(housekeeping.REPORT_KEEP + 3):
        fresh_db.save_report("notes", "may", f"notes-{i}")
    for i in range(2):
        fresh_db.save_report("lead", "may", f"lead-{i}")
    fresh_db.save_report("cto", "", "cto-0")

    with fresh_db.conn() as c:
        assert housekeeping._prune_reports(c) == 3

    with fresh_db.conn() as c:
        rows = c.execute(
            "SELECT scope, COUNT(*) n FROM reports GROUP BY scope").fetchall()
    counts = {r["scope"]: r["n"] for r in rows}
    assert counts == {"notes": housekeeping.REPORT_KEEP, "lead": 2, "cto": 1}
    assert fresh_db.latest_report("notes", "may")["content"] == \
        f"notes-{housekeeping.REPORT_KEEP + 2}"


# --- finished items ---------------------------------------------------------

def test_only_terminal_items_lose_their_working_state(fresh_db, may):
    """diff and session_id are the resume path's inputs. Clearing them on an
    item that can still be picked up would throw away the work in progress."""
    from harness import housekeeping

    for number, status in ((20, "released"), (21, "held"), (22, "working")):
        fresh_db.upsert_item("may", "issue", number, "t", "a", "open", "x")
        fresh_db.update_item("may", "issue", number, status=status,
                             diff="a diff", session_id="sess")

    with fresh_db.conn() as c:
        assert housekeeping._trim_finished_items(c) == 1

    assert fresh_db.get_item("may", "issue", 20)["diff"] == ""
    assert fresh_db.get_item("may", "issue", 20)["session_id"] == ""
    for number in (21, 22):
        item = fresh_db.get_item("may", "issue", number)
        assert item["diff"] == "a diff" and item["session_id"] == "sess"


def test_an_already_trimmed_item_is_not_counted_again(fresh_db, may):
    """The sweep runs hourly and most terminal items were trimmed long ago.
    Without the "has something left to clear" clause every one of them is
    rewritten and counted on every pass, and the summary line reports work
    that did not happen."""
    from harness import housekeeping

    fresh_db.upsert_item("may", "issue", 24, "t", "a", "open", "x")
    fresh_db.update_item("may", "issue", 24, status="released",
                         diff="a diff", session_id="sess")
    fresh_db.upsert_item("may", "issue", 25, "t", "a", "open", "x")
    fresh_db.update_item("may", "issue", 25, status="closed")

    with fresh_db.conn() as c:
        assert housekeeping._trim_finished_items(c) == 1
        assert housekeeping._trim_finished_items(c) == 0, (
            "a second sweep found work to do on already-clean items")


# --- transcript logs --------------------------------------------------------

def test_prune_files_honours_the_keep_window(tmp_path):
    """Transcripts are the record of what an agent actually did, and the run
    page reads them. LOG_KEEP_DAYS is the line: a file the wrong side of it
    goes, one inside it stays, and the directories themselves are not ours
    to remove."""
    from harness import housekeeping

    root = tmp_path / "logs"
    (root / "nested").mkdir(parents=True)
    stale = root / "nested" / "run-1.log"
    stale.write_text("old")
    _age(stale, housekeeping.LOG_KEEP_DAYS + 1)
    recent = root / "run-2.log"
    recent.write_text("new")
    _age(recent, housekeeping.LOG_KEEP_DAYS - 1)

    assert housekeeping._prune_files(root, housekeeping.LOG_KEEP_DAYS) == 1

    assert not stale.exists()
    assert recent.exists()
    assert (root / "nested").is_dir(), "only files are unlinked, not directories"


def test_prune_files_tolerates_a_missing_root(tmp_path):
    """LOG_DIR does not exist until the first agent run writes a transcript."""
    from harness import housekeeping

    assert housekeeping._prune_files(tmp_path / "never-made", 1) == 0


def test_prune_sweeps_the_configured_log_dir(fresh_db, may):
    """The wiring, not the helper: prune() must point _prune_files at
    config.LOG_DIR and report what it removed."""
    from harness import config, housekeeping

    config.LOG_DIR.mkdir(parents=True)
    stale = config.LOG_DIR / "run-1.log"
    stale.write_text("old")
    _age(stale, housekeeping.LOG_KEEP_DAYS + 1)

    summary = housekeeping.prune()

    assert not stale.exists()
    assert "1 old logs removed" in summary


# --- throwaway PR checkouts -------------------------------------------------

def test_stale_pr_checkouts_go_and_fresh_ones_stay(fresh_db, may):
    """The review flow removes its own checkout; this sweep is only for the
    ones a crash left behind. A checkout younger than PR_RUN_KEEP_HOURS may
    still have a review running in it."""
    from harness import config, housekeeping

    base = config.DATA_DIR / "pr-runs" / "may"
    stale = base / "pr-1"
    stale.mkdir(parents=True)
    _age_hours(stale, housekeeping.PR_RUN_KEEP_HOURS + 1)
    live = base / "pr-2"
    live.mkdir()

    assert housekeeping._prune_pr_runs() == 1

    assert not stale.exists()
    assert live.exists(), "a checkout the review flow is still using was deleted"


# --- SDK session transcripts ------------------------------------------------
#
# The only sweep that reaches outside DATA_DIR. Claude Code stores sessions
# under ~/.claude/projects/<cwd with / and . replaced by ->, so the scoping
# is done on the encoded directory name. Everything else in ~/.claude belongs
# to the operator, and the negative cases below are the whole guarantee.

def _encoded(path):
    """The session directory name Claude Code gives a run whose cwd is path."""
    return str(path).replace("/", "-").replace(".", "-")


@pytest.fixture()
def sessions_root(fresh_db, fake_home):
    root = fake_home / ".claude" / "projects"
    root.mkdir(parents=True)
    return root


def _session(sessions_root, cwd, name, days):
    f = sessions_root / _encoded(cwd) / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{}\n")
    _age(f, days)
    return f


def test_old_sessions_for_the_harness_own_checkouts_are_removed(
        fresh_db, sessions_root):
    """The positive case the scoping exists to allow: transcripts from the
    harness's own clones and its throwaway PR checkouts, past the window.
    Without this the negative cases below would all pass on a sweep that
    had quietly become a no-op."""
    from harness import config, housekeeping

    stale = _session(sessions_root, config.REPOS_DIR / "may", "a.jsonl",
                     housekeeping.SESSION_KEEP_DAYS + 1)
    pr_run = _session(sessions_root, config.DATA_DIR / "pr-runs" / "may" / "pr-1",
                      "b.jsonl", housekeeping.SESSION_KEEP_DAYS + 1)
    recent = _session(sessions_root, config.REPOS_DIR / "may", "c.jsonl",
                      housekeeping.SESSION_KEEP_DAYS - 1)

    assert housekeeping._prune_sdk_sessions() == 2

    assert not stale.exists() and not pr_run.exists()
    assert recent.exists(), "a session still inside the keep window was deleted"


def test_a_session_dir_outside_the_harness_checkouts_is_untouched(
        fresh_db, sessions_root):
    """The operator's own Claude Code work lives in the same directory. Only
    encoded names under REPOS_DIR or pr-runs may be swept."""
    from harness import housekeeping

    theirs = _session(sessions_root, "/home/operator/code/their-project",
                      "a.jsonl", housekeeping.SESSION_KEEP_DAYS + 30)

    assert housekeeping._prune_sdk_sessions() == 0
    assert theirs.exists(), (
        "the sweep deleted a session belonging to the operator, not the harness")


def test_only_jsonl_files_are_swept(fresh_db, sessions_root):
    """Session transcripts are *.jsonl; anything else in the directory is
    Claude Code's own state and not ours to delete."""
    from harness import config, housekeeping

    other = _session(sessions_root, config.REPOS_DIR / "may", "config.json",
                     housekeeping.SESSION_KEEP_DAYS + 30)
    todos = _session(sessions_root, config.REPOS_DIR / "may", "notes.md",
                     housekeeping.SESSION_KEEP_DAYS + 30)

    assert housekeeping._prune_sdk_sessions() == 0
    assert other.exists() and todos.exists()


def test_nothing_under_a_memory_path_is_swept(fresh_db, sessions_root):
    """Agent memory files sit under memory/ inside the session directory and
    outlive the sessions that wrote them by design."""
    from harness import config, housekeeping

    mem = _session(sessions_root, config.REPOS_DIR / "may",
                   "memory/engineering.jsonl",
                   housekeeping.SESSION_KEEP_DAYS + 30)

    assert housekeeping._prune_sdk_sessions() == 0
    assert mem.exists(), "the sweep deleted a persona memory file"


def test_a_missing_claude_dir_is_not_an_error(fresh_db):
    """The harness may well be the first thing to run on the machine."""
    from harness import housekeeping

    assert housekeeping._prune_sdk_sessions() == 0


# --- the summary line -------------------------------------------------------

def test_prune_says_nothing_when_there_is_nothing_to_say(fresh_db, may):
    """run() only writes an event when the summary is non-empty, so a quiet
    sweep must return "" rather than a string of empty clauses."""
    from harness import housekeeping

    assert housekeeping.prune() == ""


def test_the_summary_names_only_the_sweeps_that_found_something(fresh_db, may):
    """One sweep with something to report, eight with nothing: the line is
    that one clause and no empty padding around it."""
    from harness import housekeeping

    _bulk_events(fresh_db, housekeeping.EVENT_KEEP + 4)

    summary = housekeeping.prune()

    assert summary == "4 events folded"
