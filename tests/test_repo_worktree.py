"""A retried fix must not throw away the previous attempt.

add_worktree recreates the fix branch from origin/<dev> whenever a fresh
attempt starts, and a fix that fails to land goes back to "approved" for
another go — so the reset has to preserve anything the last attempt left
behind. A resume is the exception: it keeps the tree the engineer left, and
when it cannot, it says so. Real git here: the whole defect lived in what
the git commands actually do.
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


def test_a_resumed_session_is_not_handed_an_empty_tree(project, fresh_db,
                                                        monkeypatch):
    """Issue #82: fix_item calls add_worktree on every dispatch, including a
    resume. That wipes the working tree back to origin/dev before the
    resumed engineer's session is continued, so it starts from nothing while
    believing (from its own transcript) that its edits are still there.

    The previous attempt's work is preserved on a `-attempt-N` branch, but
    nothing restores it into the fresh worktree or leaves it in place — so
    the second dispatch must not hand the resumed session an empty tree."""
    import asyncio

    from harness import agents, gh, pipeline, repo

    fresh_db.upsert_item("may", "issue", 82, "t", "a", "open", "x")
    fresh_db.update_item("may", "issue", 82, status="approved", plan="do it")

    monkeypatch.setattr(gh, "issue_detail",
                        lambda repo_, number: {"number": 82, "title": "t",
                                               "body": "b"})
    monkeypatch.setattr(repo, "run_tests",
                        lambda project, cwd=None, setup=True, scratch=None:
                        (False, "tests still red"))

    calls = []

    async def fake_fix_issue(project, issue, plan, cwd, resume=None,
                             persona="Malcolm", repro_path="",
                             worktree_note=""):
        calls.append(resume)
        if len(calls) == 1:
            # First attempt: the engineer leaves its edit uncommitted, as
            # instructed ("leave changes in the working tree").
            (Path(cwd) / "fix.txt").write_text("first attempt's work\n")
            rid = fresh_db.start_run("may", "ic", "issue#82", "fix", "m",
                                     persona)
            fresh_db.finish_run(rid, True, 0.1, 1, "attempted a fix")
            return {"ok": True, "error": "", "session_id": "sess-1",
                    "output": {"success": True, "summary": "attempted a fix",
                               "docs_updated": False, "notes": "",
                               "commit_message": "fix: issue #82 (#82)"}}
        # Second attempt: a resume of the same session. The file the first
        # attempt wrote must still be here — either left in place or
        # restored from the preserved attempt branch — not wiped silently.
        assert (Path(cwd) / "fix.txt").exists(), (
            "resumed session was handed a worktree reset to origin/dev, "
            "with no trace of the previous attempt's work")
        rid = fresh_db.start_run("may", "ic", "issue#82", "fix", "m", persona)
        fresh_db.finish_run(rid, True, 0.1, 1, "gave up")
        return {"ok": True, "error": "", "session_id": "sess-1",
                "output": {"success": False, "summary": "gave up",
                           "docs_updated": False, "notes": "",
                           "commit_message": ""}}

    monkeypatch.setattr(agents, "fix_issue", fake_fix_issue)

    asyncio.run(pipeline.fix_item(project, fresh_db.get_item("may", "issue",
                                                              82)))
    first = fresh_db.get_item("may", "issue", 82)
    assert first["session_id"] == "sess-1"

    asyncio.run(pipeline.fix_item(project, first))

    assert len(calls) == 2 and calls[1] == "sess-1"  # it really was a resume


def test_a_resume_leaves_the_worktree_exactly_as_it_was(project):
    """resuming=True is the engineer coming back to its own tree: nothing is
    removed, so there is nothing to preserve and nothing to say."""
    from harness import repo
    wt, _ = repo.add_worktree(project, "harness/issue-1")
    committed = commit_in(wt, "half a fix")
    (Path(wt) / "wip.txt").write_text("and some uncommitted work")

    wt2, note = repo.add_worktree(project, "harness/issue-1", resuming=True)

    assert wt2 == wt
    assert note == ""
    assert git(wt2, "rev-parse", "HEAD").strip() == committed
    assert (Path(wt2) / "fix.txt").read_text() == "half a fix"
    assert (Path(wt2) / "wip.txt").exists()
    assert git(repo.repo_dir(project), "branch", "--list",
               "harness/issue-1-attempt-*").strip() == ""


def test_a_resume_with_no_worktree_left_says_so_and_keeps_the_work(project):
    """The fallback: the data dir was wiped under the session, so the tree
    has to be recreated after all. The note must say plainly that it was
    reset and name the branch the earlier work was parked on."""
    from harness import repo
    wt, _ = repo.add_worktree(project, "harness/issue-1")
    lost = commit_in(wt, "half a fix")
    subprocess.run(["rm", "-rf", str(wt)], check=True)

    wt2, note = repo.add_worktree(project, "harness/issue-1", resuming=True)

    assert note.startswith(repo.RESUMED_INTO_RESET)
    assert "origin/dev" in note and "harness/issue-1-attempt-1" in note
    assert tip(project, "harness/issue-1-attempt-1") == lost
    assert (Path(wt2) / "fix.txt").exists() is False


def test_a_resumed_engineer_is_told_when_its_tree_was_reset(project, fresh_db,
                                                            monkeypatch):
    """And the reset reaches the engineer itself, not just the thread: a
    resumed session reads its own transcript, so the prompt has to say the
    edits are gone."""
    import asyncio

    from harness import agents, gh, pipeline, repo

    fresh_db.upsert_item("may", "issue", 82, "t", "a", "open", "x")
    fresh_db.update_item("may", "issue", 82, status="approved", plan="do it",
                         session_id="sess-1")

    monkeypatch.setattr(gh, "issue_detail",
                        lambda repo_, number: {"number": 82, "title": "t",
                                               "body": "b"})
    monkeypatch.setattr(repo, "run_tests",
                        lambda project, cwd=None, setup=True, scratch=None:
                        (False, "tests still red"))
    # The worktree the saved session worked in is gone (a wiped data dir).
    wt, _ = repo.add_worktree(project, "harness/issue-82")
    commit_in(wt, "half a fix")
    subprocess.run(["rm", "-rf", str(wt)], check=True)

    seen = {}

    async def fake_fix_issue(project, issue, plan, cwd, resume=None,
                             persona="Malcolm", repro_path="",
                             worktree_note=""):
        seen["note"] = worktree_note
        rid = fresh_db.start_run("may", "ic", "issue#82", "fix", "m", persona)
        fresh_db.finish_run(rid, True, 0.1, 1, "gave up")
        return {"ok": True, "error": "", "session_id": "sess-1",
                "output": {"success": False, "summary": "gave up",
                           "docs_updated": False, "notes": "",
                           "commit_message": ""}}

    monkeypatch.setattr(agents, "fix_issue", fake_fix_issue)
    asyncio.run(pipeline.fix_item(project,
                                  fresh_db.get_item("may", "issue", 82)))

    assert seen["note"].startswith(repo.RESUMED_INTO_RESET)
    assert "harness/issue-82-attempt-1" in seen["note"]
    assert "git status" in seen["note"]
    # ...and the same sentence is on the thread for a human to find.
    assert any("harness/issue-82-attempt-1" in r["text"]
               for r in fresh_db.thread("may", "issue#82"))


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
