"""Housekeeping's worktree sweep must not delete work still in play.

_prune_worktrees (issue #96) walked DATA_DIR/worktrees/<project>/ and
removed any directory idle past WORKTREE_KEEP_DAYS on mtime alone, with no
regard for whether an item was still going to resume into it. An item held
awaiting an operator or Harry answer sits idle for exactly that reason --
past three days the pruner deleted its worktree out from under it, and the
next resume started the engineer from scratch. This is the same class of
loss as #82, arriving by a different route.
"""
import os
import time

import pytest


def _age(path, days):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


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
