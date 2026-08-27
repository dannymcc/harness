"""Project-supplied commands must not see harness's credentials.

setup_command and test_command are the project's own build hooks, so on a
community PR they are the contributor's code. These tests run them for real
(bash, git — no network) and check what they can reach.
"""
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def project(fresh_db, monkeypatch):
    """A project whose test runs use the ambient python, not a fresh venv.

    Building a venv per test would add seconds and prove nothing: the point
    here is the environment the command lands in.
    """
    from harness import repo
    monkeypatch.setattr(repo, "_venv_python",
                        lambda project, vdir=None: Path(sys.executable))
    monkeypatch.setenv("GH_TOKEN", "ghp_secret_token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-secret")
    from harness import config
    fresh_db.create_project("may", "example/may")
    (config.REPOS_DIR / "may").mkdir(parents=True, exist_ok=True)
    return dict(fresh_db.get_project("may"))


def test_test_command_cannot_read_the_github_token(project):
    from harness import repo
    project["test_command"] = 'echo "seen=[$GH_TOKEN][$CLAUDE_CODE_OAUTH_TOKEN]"'
    passed, out = repo.run_tests(project, setup=False)
    assert passed
    assert "seen=[][]" in out
    assert "ghp_secret_token" not in out and "sk-ant-secret" not in out


def test_setup_command_cannot_read_the_github_token(project):
    from harness import repo
    project["setup_command"] = 'echo "setup=[$GH_TOKEN]"'
    project["test_command"] = "true"
    passed, out = repo.run_tests(project)
    assert passed and "setup=[]" in out


def test_ensure_test_env_is_sandboxed_too(project):
    from harness import config, repo
    marker = config.REPOS_DIR / "may" / "leaked"
    project["setup_command"] = f'printenv GH_TOKEN > "{marker}"; true'
    repo.ensure_test_env(project)
    assert marker.read_text() == ""


def test_home_points_at_scratch_not_the_harness_home(project):
    """~/.config/gh/hosts.yml, ~/.claude and ~/.git-credentials live in the
    real HOME; the run must not be able to reach them."""
    from harness import config, repo
    project["test_command"] = "echo HOME=$HOME TMPDIR=$TMPDIR"
    passed, out = repo.run_tests(project, setup=False)
    scratch = config.DATA_DIR / "sandbox" / "may"
    assert passed
    assert f"HOME={scratch}" in out and f"TMPDIR={scratch / 'tmp'}" in out


def test_a_normal_run_still_passes_and_reports_output(project):
    from harness import repo
    project["test_command"] = "echo '3 passed'"
    passed, out = repo.run_tests(project, setup=False)
    assert passed and "3 passed" in out
    project["test_command"] = "echo '1 failed'; exit 1"
    passed, out = repo.run_tests(project, setup=False)
    assert not passed and "1 failed" in out


def test_a_hung_test_command_returns_a_failure_not_a_crash(project, monkeypatch):
    """A hung command comes out of gh.run as CmdTimeout, a CmdError, so
    run_tests catches it with everything else — but the verdict must stay a
    (False, tail) verdict every caller (review, release, merge) relies on,
    not an unhandled exception (#102), and the tail must still read as a
    hang rather than an ordinary failure (#110). The partial output the
    timeout carries should end up in the tail too."""
    from harness import repo
    from harness.gh import CmdTimeout

    def _timeout(cmd, cwd=None, check=True, timeout=600, env=None):
        raise CmdTimeout(cmd, timeout,
                         out="partial output before it hung\n")

    monkeypatch.setattr(repo, "run", _timeout)
    project["test_command"] = "this is never actually run — run() is stubbed"
    passed, out = repo.run_tests(project, setup=False)
    assert passed is False
    assert "timeout" in out.lower() or "timed out" in out.lower()
    assert "partial output before it hung" in out


def test_a_hung_setup_command_is_swallowed_like_a_failed_one(project,
                                                             monkeypatch):
    """ensure_test_env already runs setup with check=False: a setup that
    fails is not worth stopping the fix wave for, and one that hangs is no
    different. The test runs that follow report the real damage."""
    from harness import repo
    from harness.gh import CmdTimeout

    def _timeout(cmd, cwd=None, check=True, timeout=600, env=None):
        raise CmdTimeout(cmd, timeout)

    monkeypatch.setattr(repo, "run", _timeout)
    project["setup_command"] = "this is never actually run — run() is stubbed"
    repo.ensure_test_env(project)      # must not raise


# --- the throwaway PR checkout ----------------------------------------------

@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    """Fresh clones made below commit without a repo-local identity; CI
    runners have no global one, so supply it through the environment."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "T")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "T")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def origin(tmp_path):
    """A local bare repo with a dev branch and a refs/pull/1/head, so the PR
    flow can be exercised without a network or gh."""
    work, bare = tmp_path / "work", tmp_path / "origin.git"
    work.mkdir()
    _git("init", "-q", "-b", "dev", cwd=work)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("config", "user.name", "T", cwd=work)
    (work / "README.md").write_text("hello\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "base", cwd=work)
    _git("checkout", "-qb", "contrib", cwd=work)
    (work / "contributed.txt").write_text("from the PR\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "contribution", cwd=work)
    _git("checkout", "-q", "dev", cwd=work)
    _git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
    head = subprocess.run(["git", "rev-parse", "contrib"], cwd=work,
                          capture_output=True, text=True).stdout.strip()
    _git("update-ref", "refs/pull/1/head", head, cwd=bare)
    _git("branch", "-D", "contrib", cwd=bare)
    return bare


def test_pr_code_is_tested_in_a_disposable_clone(project, origin):
    """The merged PR is tested outside harness's clone, and the directory is
    gone afterwards."""
    from harness import repo
    clone = repo.repo_dir(project)
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)

    checkout = repo.fetch_pr_branch(project, 1, "harness/pr-1")
    assert checkout == repo.pr_run_dir(project, 1) / "repo"
    assert (checkout / "contributed.txt").read_text() == "from the PR\n"
    assert checkout != clone and not str(checkout).startswith(str(clone))

    # Its .git is its own, and no hook the clone might carry can fire in it.
    hooks = subprocess.run(["git", "config", "core.hooksPath"], cwd=checkout,
                           capture_output=True, text=True).stdout.strip()
    assert hooks and list(Path(hooks).iterdir()) == []
    assert not (checkout / ".git").is_file()   # not a worktree pointer

    project["test_command"] = 'echo "run=[$GH_TOKEN]"; cat contributed.txt'
    passed, out = repo.run_pr_tests(project, 1)
    assert passed and "run=[]" in out and "from the PR" in out

    repo.remove_pr_run(project, 1)
    assert not repo.pr_run_dir(project, 1).exists()
    # harness's own clone is untouched by any of it
    assert (clone / ".git").is_dir()
    assert not (clone / ".git" / "hooks" / "pre-commit").exists()


# --- landing on a moved dev: the failure paths must not strand the fix -----

def test_failed_land_pushes_a_safety_branch_and_says_so(project, origin, tmp_path):
    """If push_worktree_to_dev can't land the commit on dev (here: the rebase
    onto a moved dev conflicts), the commit must not be left existing only in
    the worktree on this box. It has to be pushed to a remote branch (named
    after the `branch` argument already passed in) before the failure is
    reported, and the returned error has to name that branch so a human can
    find the work."""
    from harness import repo

    branch = "harness/issue-9"
    wt = tmp_path / "wt"
    _git("clone", "-q", str(origin), str(wt), cwd=tmp_path)
    _git("checkout", "-q", "-b", branch, "origin/dev", cwd=wt)
    (wt / "README.md").write_text("mine\n")
    _git("add", "-A", cwd=wt)
    _git("commit", "-qm", "fix: mine", cwd=wt)
    local_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt,
        capture_output=True, text=True).stdout.strip()

    # Someone else lands a conflicting change on dev first, so the rebase
    # below can't go cleanly.
    other = tmp_path / "other"
    _git("clone", "-q", str(origin), str(other), cwd=tmp_path)
    _git("checkout", "-q", "dev", cwd=other)
    (other / "README.md").write_text("theirs\n")
    _git("add", "-A", cwd=other)
    _git("commit", "-qm", "conflicting change", cwd=other)
    _git("push", "-q", "origin", "dev", cwd=other)

    ok, err = repo.push_worktree_to_dev(project, wt, branch)

    assert not ok
    assert "conflicted" in err
    # The error must name where the work went...
    assert branch in err
    # ...because it actually has to be on origin.
    _git("fetch", "-q", "origin", cwd=wt)
    remote_head = subprocess.run(
        ["git", "rev-parse", f"origin/{branch}"], cwd=wt,
        capture_output=True, text=True).stdout.strip()
    assert remote_head == local_head


def test_a_failed_safety_push_is_reported_as_such(project, origin, tmp_path):
    """If the fix cannot even be parked on its own branch, the error must say
    so distinctly — that is the one case where the commit exists nowhere but
    this box, and it must not read like a tidy hand-off."""
    from harness import repo

    branch = "harness/issue-9"
    wt = tmp_path / "wt"
    _git("clone", "-q", str(origin), str(wt), cwd=tmp_path)
    _git("checkout", "-q", "-b", branch, "origin/dev", cwd=wt)
    (wt / "README.md").write_text("mine\n")
    _git("add", "-A", cwd=wt)
    _git("commit", "-qm", "fix: mine", cwd=wt)

    other = tmp_path / "other"
    _git("clone", "-q", str(origin), str(other), cwd=tmp_path)
    _git("checkout", "-q", "dev", cwd=other)
    (other / "README.md").write_text("theirs\n")
    _git("add", "-A", cwd=other)
    _git("commit", "-qm", "conflicting change", cwd=other)
    _git("push", "-q", "origin", "dev", cwd=other)

    # Origin now refuses every push (a protected ref, a wedged remote, no
    # credentials — from here they all look the same).
    hook = origin / "hooks" / "pre-receive"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    ok, err = repo.push_worktree_to_dev(project, wt, branch)

    assert not ok
    assert "conflicted" in err
    assert repo.SAFETY_PUSH_FAILED in err
    assert "for a human to pick up" not in err
    assert subprocess.run(["git", "rev-parse", "--verify", "-q",
                           f"refs/heads/{branch}"], cwd=origin,
                          capture_output=True).returncode != 0


def test_a_hung_push_parks_the_fix_like_a_failed_one(project, origin,
                                                     tmp_path, monkeypatch):
    """A git that hangs used to come out of push_worktree_to_dev instead of
    parking the commit — the item stayed 'working', the cycle died and a
    tested commit was left only in the worktree on this box (#102, #108).
    It now arrives as CmdTimeout, and the clause that handles it has to stay
    ahead of the general `except CmdError` (#110): a hang parks and reports
    like any other failure to land, names itself as a hang, and does not go
    round the retry loop that exists for a push a moved dev rejected."""
    from harness import repo
    from harness.gh import CmdTimeout

    branch = "harness/issue-10"
    wt = tmp_path / "wt"
    _git("clone", "-q", str(origin), str(wt), cwd=tmp_path)
    _git("checkout", "-q", "-b", branch, "origin/dev", cwd=wt)
    (wt / "README.md").write_text("mine\n")
    _git("add", "-A", cwd=wt)
    _git("commit", "-qm", "fix: mine", cwd=wt)
    local_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt,
        capture_output=True, text=True).stdout.strip()

    real_run, attempts = repo.run, []

    def _hang_on_the_push_to_dev(cmd, cwd=None, check=True, timeout=600,
                                 env=None):
        if cmd[:2] == ["git", "push"] and "--force" not in cmd:
            attempts.append(cmd)
            raise CmdTimeout(cmd, timeout, out="Enumerating objects\n")
        return real_run(cmd, cwd=cwd, check=check, timeout=timeout, env=env)

    monkeypatch.setattr(repo, "run", _hang_on_the_push_to_dev)

    ok, err = repo.push_worktree_to_dev(project, wt, branch)

    assert not ok
    assert "timed out after 600s" in err          # a hang, said as a hang
    assert "Enumerating objects" in err           # the partial output kept
    assert repo.SAFETY_PUSH_FAILED not in err
    assert len(attempts) == 1                     # not retried as a rejection

    # The commit is on origin/<branch>, where the error says it is.
    assert branch in err
    _git("fetch", "-q", "origin", cwd=wt)
    remote_head = subprocess.run(
        ["git", "rev-parse", f"origin/{branch}"], cwd=wt,
        capture_output=True, text=True).stdout.strip()
    assert remote_head == local_head


def test_a_hung_rebase_is_reported_as_a_hang_not_a_conflict(project, origin,
                                                            tmp_path,
                                                            monkeypatch):
    """The sharp edge of #110: `except CmdTimeout` has to sit *before* the
    `except CmdError` that reports a conflicted rebase, because CmdTimeout
    is a CmdError. Ordered the other way round the general clause swallows
    it and the operator is told the rebase conflicted — a claim about the
    code that nothing actually established."""
    from harness import repo
    from harness.gh import CmdTimeout

    branch = "harness/issue-11"
    wt = tmp_path / "wt"
    _git("clone", "-q", str(origin), str(wt), cwd=tmp_path)
    _git("checkout", "-q", "-b", branch, "origin/dev", cwd=wt)
    (wt / "README.md").write_text("mine\n")
    _git("add", "-A", cwd=wt)
    _git("commit", "-qm", "fix: mine", cwd=wt)

    real_run = repo.run

    def _hang_on_the_rebase(cmd, cwd=None, check=True, timeout=600, env=None):
        if cmd[:2] == ["git", "rebase"] and "--abort" not in cmd:
            raise CmdTimeout(cmd, timeout, out="First, rewinding head\n")
        if cmd[:3] == ["git", "rev-list", "--count"] and "HEAD.." in cmd[3]:
            return "1\n"               # dev moved: take the rebase branch
        return real_run(cmd, cwd=cwd, check=check, timeout=timeout, env=env)

    monkeypatch.setattr(repo, "run", _hang_on_the_rebase)

    ok, err = repo.push_worktree_to_dev(project, wt, branch)

    assert not ok
    assert "the rebase onto moved dev timed out after 600s" in err
    assert "conflicted" not in err
    assert "First, rewinding head" in err        # the partial output kept
