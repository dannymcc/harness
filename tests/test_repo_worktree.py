"""A retried fix must not throw away the previous attempt.

add_worktree recreates the fix branch from origin/<dev> on every dispatch,
and a fix that fails to land goes back to "approved" for another go — so the
reset has to preserve anything the last attempt left behind. Real git here:
the whole defect lived in what the git commands actually do.
"""
import subprocess
from pathlib import Path

import pytest

from harness.gh import run


def git(cwd, *args, **kw):
    return run(["git", *args], cwd=cwd, **kw)


@pytest.fixture()
def project(fresh_db, monkeypatch):
    """A project whose origin is a bare repo on disk, cloned like harness's."""
    from harness import config, repo
    origin = config.DATA_DIR / "origin.git"
    seed = config.DATA_DIR / "seed"
    seed.mkdir(parents=True)
    git(seed, "init", "-q", "-b", "dev")
    (seed / "README.md").write_text("hello\n")
    git(seed, "add", "-A")
    git(seed, "-c", "user.email=t@example.com", "-c", "user.name=T",
        "commit", "-qm", "initial")
    git(seed, "clone", "-q", "--bare", str(seed), str(origin))

    fresh_db.create_project("may", "example/may")
    proj = dict(fresh_db.get_project("may"))
    d = repo.repo_dir(proj)
    d.parent.mkdir(parents=True, exist_ok=True)
    git(config.DATA_DIR, "clone", "-q", str(origin), str(d))
    git(d, "config", "user.email", "harness@example.com")
    git(d, "config", "user.name", "Harness")
    return proj


def tip(project, ref):
    from harness import repo
    return git(repo.repo_dir(project), "rev-parse", ref).strip()


def commit_in(wt, text, message="an attempt at the fix"):
    (Path(wt) / "fix.txt").write_text(text)
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", message)
    return git(wt, "rev-parse", "HEAD").strip()


def test_first_dispatch_cuts_the_branch_from_dev(project):
    from harness import repo
    wt, note = repo.add_worktree(project, "harness/issue-1")
    assert note == ""
    assert git(wt, "rev-parse", "HEAD").strip() == tip(project, "origin/dev")


def test_retry_preserves_commits_the_previous_attempt_made(project):
    from harness import repo
    wt, _ = repo.add_worktree(project, "harness/issue-1")
    lost = commit_in(wt, "half a fix")

    wt2, note = repo.add_worktree(project, "harness/issue-1")

    # The branch is back on dev for the new attempt...
    assert git(wt2, "rev-parse", "HEAD").strip() == tip(project, "origin/dev")
    # ...but the commit is still reachable, and the note says where.
    assert tip(project, "harness/issue-1-attempt-1") == lost
    assert "harness/issue-1-attempt-1" in note
    assert (Path(wt2) / "fix.txt").exists() is False


def test_uncommitted_work_is_committed_before_the_worktree_is_removed(project):
    from harness import repo
    wt, _ = repo.add_worktree(project, "harness/issue-1")
    (Path(wt) / "fix.txt").write_text("not committed yet")

    _, note = repo.add_worktree(project, "harness/issue-1")

    saved = tip(project, "harness/issue-1-attempt-1")
    assert "harness/issue-1-attempt-1" in note
    assert "fix.txt" in git(repo.repo_dir(project), "show", "--name-only",
                            "--format=", saved)


def test_a_third_attempt_gets_its_own_ref(project):
    from harness import repo
    wt, _ = repo.add_worktree(project, "harness/issue-1")
    first = commit_in(wt, "attempt one")
    wt2, _ = repo.add_worktree(project, "harness/issue-1")
    second = commit_in(wt2, "attempt two")

    _, note = repo.add_worktree(project, "harness/issue-1")

    assert tip(project, "harness/issue-1-attempt-1") == first
    assert tip(project, "harness/issue-1-attempt-2") == second
    assert "harness/issue-1-attempt-2" in note


def test_nothing_is_saved_when_the_branch_holds_nothing_new(project):
    """A dispatch that made no commit leaves no ref and no thread noise."""
    from harness import repo
    repo.add_worktree(project, "harness/issue-1")
    _, note = repo.add_worktree(project, "harness/issue-1")
    assert note == ""
    assert git(repo.repo_dir(project), "branch", "--list",
               "harness/issue-1-attempt-*").strip() == ""


def test_work_already_on_dev_is_not_preserved_again(project):
    """The usual happy path: the fix landed, so the reset loses nothing."""
    from harness import repo
    wt, _ = repo.add_worktree(project, "harness/issue-1")
    commit_in(wt, "the fix")
    git(wt, "push", "-q", "origin", "HEAD:dev")

    _, note = repo.add_worktree(project, "harness/issue-1")

    assert note == ""
    assert git(repo.repo_dir(project), "branch", "--list",
               "harness/issue-1-attempt-*").strip() == ""


def test_a_retry_that_added_nothing_reuses_the_existing_ref(project):
    from harness import repo
    wt, _ = repo.add_worktree(project, "harness/issue-1")
    first = commit_in(wt, "attempt one")
    repo.add_worktree(project, "harness/issue-1")
    # Second retry: the branch is back on dev, so there is nothing new to
    # save — and no attempt-2 duplicating attempt-1.
    _, note = repo.add_worktree(project, "harness/issue-1")

    assert tip(project, "harness/issue-1-attempt-1") == first
    with pytest.raises(Exception):
        tip(project, "harness/issue-1-attempt-2")
    assert note == ""


def test_the_recovery_ref_survives_a_removed_worktree_directory(project):
    """A wiped data dir leaves a stale registration; the commit is still on
    the branch and must still be preserved."""
    from harness import repo
    wt, _ = repo.add_worktree(project, "harness/issue-1")
    lost = commit_in(wt, "half a fix")
    subprocess.run(["rm", "-rf", str(wt)], check=True)

    _, note = repo.add_worktree(project, "harness/issue-1")

    assert tip(project, "harness/issue-1-attempt-1") == lost
    assert "harness/issue-1-attempt-1" in note
