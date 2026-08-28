"""Manage harness's clone of each project and run its test suite there.

The clone under data/repos/<project> belongs to harness: agents edit it, the
pipeline resets it. It is never the user's own working copy.
"""
import asyncio
import fcntl
import os
import re
import shlex
import shutil
import sys
import time
import venv
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from . import config, db
from .gh import run, CmdError, CmdTimeout

# How long a wait for the clone lock has to drag on before it is worth an
# event. Short enough that a stuck holder shows up on the board while it is
# still stuck, long enough that ordinary contention (an engineer's worktree
# bookkeeping, a push) never says anything.
LOCK_WAIT_LOG_SECONDS = 5.0


def _say(message: str, level: str, name: str) -> None:
    try:
        db.log_event(message, level, project=name)
    except Exception:   # a note about a wait must never break the wait
        pass


def _take_flock(fh, project) -> None:
    """Block until the exclusive flock is ours, saying so if it takes a while.

    Polling rather than a bare blocking flock() so a wait that is going
    nowhere is visible: the holder can be another engineer mid-push or a
    salvage script attached over docker exec, and in the untimed version
    every such wait was silent — a stuck holder simply looked like a stuck
    harness.
    """
    name = project["name"]
    started = time.monotonic()
    told = False
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            waited = time.monotonic() - started
            if not told and waited >= LOCK_WAIT_LOG_SECONDS:
                told = True
                _say(f"Waiting on the clone lock for {name} — something else "
                     f"has held it for {int(waited)}s", "warn", name)
            time.sleep(0.05)
    if told:
        _say(f"Took the clone lock for {name} after "
             f"{int(time.monotonic() - started)}s", "info", name)


@contextmanager
def clone_lock(project):
    """Cross-process lock over a project's clone.

    Everything that mutates the checkout (pipeline cycles, salvage or other
    maintenance scripts) must hold this. flock, so it works across docker
    exec sessions, not just threads.

    Blocking, and deliberately so — but that means it must never be entered
    from the worker's event loop thread, where the wait would freeze every
    other desk, the attendant and the heartbeat with it. Coroutines take it
    through clone_lock_async, or through a plain function they hand to
    asyncio.to_thread.
    """
    config.REPOS_DIR.mkdir(parents=True, exist_ok=True)
    lockfile = config.REPOS_DIR / f".{project['name']}.lock"
    with open(lockfile, "w") as fh:
        _take_flock(fh, project)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@asynccontextmanager
async def clone_lock_async(project):
    """clone_lock for a coroutine that must hold it across an await.

    The same flock, with the same scope — only the waiting happens on a
    worker thread, so the loop keeps running while another process (or
    another engineer) has the clone. For work that does not span an await,
    prefer putting the whole locked block in a plain function and calling it
    through asyncio.to_thread.
    """
    cm = clone_lock(project)
    await asyncio.to_thread(cm.__enter__)
    try:
        yield
    finally:
        # Cancelled mid-wait, the acquiring thread cannot be interrupted: it
        # finishes, and the lock is dropped when the context manager it left
        # behind is collected (its finally unlocks, and the fd closes).
        await asyncio.to_thread(cm.__exit__, None, None, None)


def repo_dir(project) -> Path:
    return config.REPOS_DIR / project["name"]


def ensure_clone(project) -> Path:
    d = repo_dir(project)
    if not (d / ".git").exists():
        config.REPOS_DIR.mkdir(parents=True, exist_ok=True)
        run(["gh", "repo", "clone", project["repo"], str(d)])
    return d


def clean_checkout(project, branch: str) -> Path:
    """Fetch, hard-reset to origin/<branch>. Discards any local mess."""
    d = ensure_clone(project)
    run(["git", "fetch", "origin", "--prune"], cwd=d)
    run(["git", "checkout", "-f", branch], cwd=d)
    run(["git", "reset", "--hard", f"origin/{branch}"], cwd=d)
    run(["git", "clean", "-fd"], cwd=d)
    return d


def create_branch(project, name: str, base: str) -> Path:
    d = clean_checkout(project, base)
    run(["git", "checkout", "-B", name, f"origin/{base}"], cwd=d)
    return d


def diff_stat(project, base: str) -> tuple[str, str]:
    """Return (stat, full_diff vs origin/<base>) for the current branch."""
    d = repo_dir(project)
    stat = run(["git", "diff", "--stat", f"origin/{base}"], cwd=d)
    diff = run(["git", "diff", f"origin/{base}"], cwd=d)
    if len(diff) > 150_000:
        diff = diff[:150_000] + "\n... [diff truncated] ..."
    return stat, diff


def commit_log(project, base: str) -> str:
    d = repo_dir(project)
    return run(["git", "log", "--oneline", f"origin/{base}..HEAD"], cwd=d)


def has_changes(project, base: str) -> bool:
    d = repo_dir(project)
    unstaged = run(["git", "status", "--porcelain"], cwd=d).strip()
    ahead = run(["git", "rev-list", "--count", f"origin/{base}..HEAD"], cwd=d).strip()
    return bool(unstaged) or ahead != "0"


def dev_ahead_count(project) -> int:
    """Commits on origin/dev that origin/main does not have.

    Read-only and lock-free: the cycle has already fetched by the time this
    is asked, and a count one fetch stale only ever costs a cycle's delay.
    Returns 0 if the clone is missing or git fails — never a false "there is
    something to release". A page render asks it too, so the timeout is
    short: a wedged git must not hold the GUI open.
    """
    try:
        out = run(["git", "rev-list", "--count",
                   f"origin/{project['main_branch']}..origin/{project['dev_branch']}"],
                  cwd=repo_dir(project), timeout=15)
        return int(out.strip() or 0)
    except (CmdError, ValueError, OSError):
        return 0


def commit_all(project, message: str) -> None:
    d = repo_dir(project)
    run(["git", "add", "-A"], cwd=d)
    run(["git", "commit", "-m", message], cwd=d)


def push_branch_to(project, local_branch: str, remote_branch: str) -> None:
    d = repo_dir(project)
    run(["git", "push", "origin", f"{local_branch}:{remote_branch}"], cwd=d)


def worktrees_dir(project) -> Path:
    return config.DATA_DIR / "worktrees" / project["name"]


def _ref_tip(d: Path, ref: str) -> str:
    """The commit a ref points at, or "" if there is no such ref."""
    return run(["git", "rev-parse", "--verify", "--quiet", ref],
               cwd=d, check=False).strip()


def _preserve_previous_attempt(project, d: Path, branch: str,
                               wt: Path) -> str:
    """Save whatever an earlier attempt left on <branch> before it is reset.

    add_worktree recreates the branch from origin/<dev>, and a fix that fails
    to land is re-dispatched, so without this any commit the previous attempt
    made — and any uncommitted work in its worktree — would go silently. Work
    origin/<dev> already contains is not worth a ref: resetting to it loses
    nothing.

    Returns a sentence for the item thread naming where the work went, or ""
    when the previous attempt left nothing that a reset would destroy. Raises
    rather than let the caller reset a branch whose tip could not be saved.
    """
    dev = f"origin/{project['dev_branch']}"
    note = ""
    if (wt / ".git").exists() and run(["git", "status", "--porcelain"],
                                      cwd=wt, check=False).strip():
        # About to be `worktree remove --force`d: commit it or lose it.
        try:
            run(["git", "add", "-A"], cwd=wt)
            run(["git", "commit", "-m",
                 f"wip: uncommitted work from an earlier attempt on {branch}"],
                cwd=wt)
        except (CmdError, OSError) as e:
            note = ("Uncommitted changes in the previous worktree could not "
                    f"be committed ({str(e)[:200]}) and went with it. ")
    tip = _ref_tip(d, f"refs/heads/{branch}")
    if not tip:
        return note.strip()
    # Anything but a clean "0" (including a git error) counts as work worth
    # keeping: guessing wrong here is what loses commits.
    extra = run(["git", "rev-list", "--count", tip, f"^{dev}"],
                cwd=d, check=False).strip()
    if extra.isdigit() and int(extra) == 0:
        return note.strip()
    saved = run(["git", "for-each-ref",
                 "--format=%(objectname) %(refname:short)",
                 f"refs/heads/{branch}-attempt-*"], cwd=d, check=False)
    for line in saved.splitlines():
        obj, _, name = line.partition(" ")
        if obj == tip:  # a retry that added nothing; already preserved
            return (note + f"The previous attempt is still on {name}.").strip()
    taken = {line.partition(" ")[2] for line in saved.splitlines()}
    n = 1
    while f"{branch}-attempt-{n}" in taken:
        n += 1
    recovery = f"{branch}-attempt-{n}"
    run(["git", "branch", recovery, tip], cwd=d)
    return (note + f"The previous attempt's commits were preserved on "
                   f"{recovery} ({tip[:8]}) before the branch was reset to "
                   f"{dev}.").strip()


def _worktree_is_live(d: Path, wt: Path, branch: str) -> bool:
    """True when wt is a working worktree of clone d with <branch> on HEAD.

    Anything else — no directory, a stale .git pointing at a wiped clone, a
    different branch checked out — is not something a session can be resumed
    into, so the caller recreates it instead.
    """
    if not (wt / ".git").exists():
        return False
    head = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt,
               check=False).strip()
    if head != branch:
        return False
    listed = run(["git", "worktree", "list", "--porcelain"], cwd=d,
                 check=False)
    here = os.path.realpath(wt)
    return any(os.path.realpath(line[len("worktree "):]) == here
               for line in listed.splitlines()
               if line.startswith("worktree "))


# Opening words of the note add_worktree returns when a resume had to be
# given a fresh worktree after all. pipeline.py matches on it to put the
# same warning in front of the engineer as well as on the item thread —
# keep the two in step.
RESUMED_INTO_RESET = "The worktree for this item was gone, so it was recreated"


def add_worktree(project, branch: str,
                 resuming: bool = False) -> tuple[Path, str]:
    """Create (or recreate) an isolated worktree for one fix branch.

    Holds the clone lock only for the brief git bookkeeping; afterwards the
    worktree is independent and agents can work there without contending
    for the main checkout.

    Pass resuming=True when an engineer's saved session is about to be
    continued in this tree. Recreating it would hand that session an empty
    checkout while its own transcript still says the edits are there, so a
    live worktree with <branch> checked out is left exactly as the engineer
    left it — the branch belongs to this one item, so isolation is unchanged.
    Only when no usable worktree survives (a wiped data dir, a broken
    registration) does a resume fall back to a fresh one, and the note then
    says so in as many words — see RESUMED_INTO_RESET.

    Returns (worktree, note): the note is a sentence about work an earlier
    attempt left behind and where it was saved, empty when there was none.
    The caller puts it on the item thread — see _preserve_previous_attempt."""
    with clone_lock(project):
        d = ensure_clone(project)
        run(["git", "fetch", "origin", "--prune"], cwd=d)
        wt = worktrees_dir(project) / branch.replace("/", "-")
        if resuming and _worktree_is_live(d, wt, branch):
            return wt, ""
        note = _preserve_previous_attempt(project, d, branch, wt)
        if wt.exists():
            run(["git", "worktree", "remove", "--force", str(wt)], cwd=d,
                check=False)
        # A deleted worktree dir leaves a stale registration that makes
        # `worktree add -B` refuse forever; prune first.
        run(["git", "worktree", "prune"], cwd=d, check=False)
        wt.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "worktree", "add", "-B", branch, str(wt),
             f"origin/{project['dev_branch']}"], cwd=d)
        if resuming:
            note = (f"{RESUMED_INTO_RESET} from origin/"
                    f"{project['dev_branch']}: every edit the earlier attempt "
                    f"left in it is gone. " + note).strip()
        return wt, note


def remove_worktree(project, wt: Path) -> None:
    with clone_lock(project):
        d = ensure_clone(project)
        run(["git", "worktree", "remove", "--force", str(wt)], cwd=d,
            check=False)


def _is_screenshot(status_line: str) -> bool:
    """Is this `git status --porcelain` line one of the run's screenshots?

    Normally there is nothing to filter — ensure_screenshot_dir excludes them
    and git never mentions them. This is for the run where that failed: a
    directory of PNGs must not make an engineer who changed nothing look like
    one who did. Git collapses an untracked directory to `?? .harness/`, so
    that spelling counts too.
    """
    path = status_line[3:].strip().strip('"')
    return path.startswith(SCREENSHOT_DIR) or path == _EXCLUDE_LINE


def wt_has_changes(project, wt: Path) -> bool:
    unstaged = [line for line in
                run(["git", "status", "--porcelain"], cwd=wt).splitlines()
                if line.strip() and not _is_screenshot(line)]
    ahead = run(["git", "rev-list", "--count",
                 f"origin/{project['dev_branch']}..HEAD"], cwd=wt).strip()
    return bool(unstaged) or ahead != "0"


def wt_commit_all(project, wt: Path, message: str) -> None:
    run(["git", "add", "-A"], cwd=wt)
    # The screenshots an engineer renders are evidence for the run, not files
    # the project asked for. ensure_screenshot_dir excludes them so they never
    # reach the index; this is the braces to that belt, because `git add -A`
    # would otherwise push a directory of PNGs to dev the one time it fails.
    run(["git", "reset", "-q", "--", SCREENSHOT_DIR], cwd=wt, check=False)
    run(["git", "commit", "-m", message], cwd=wt)


def wt_diff(project, wt: Path) -> tuple[str, str]:
    base = f"origin/{project['dev_branch']}"
    stat = run(["git", "diff", "--stat", base], cwd=wt)
    diff = run(["git", "diff", base], cwd=wt)
    if len(diff) > 150_000:
        diff = diff[:150_000] + "\n... [diff truncated] ..."
    return stat, diff


# Marker for "the fix could not be parked on its own branch either", so
# pipeline.py can raise its own warn event for the one case where the work
# really is only on this box. Keep the two in step.
SAFETY_PUSH_FAILED = "the safety push failed too"


def _park_on_branch(project, wt: Path, branch: str, reason: str,
                    detail: str = "") -> str:
    """Push a fix that cannot land on dev to origin/<branch> instead.

    The commit has already passed the deterministic gate, and the next
    dispatch for the item recreates the worktree from origin/dev, so a
    commit left only here is a commit about to be destroyed. Forced,
    because the branch is harness's own scratch for this one item and the
    attempt being reported is the current one.

    Returns the failure reason with where the work went appended — the
    caller hands that straight back as its error string.
    """
    try:
        with clone_lock(project):
            run(["git", "push", "--force", "origin",
                 f"HEAD:refs/heads/{branch}"], cwd=wt)
        where = f"pushed to origin/{branch} for a human to pick up"
    except (CmdError, OSError) as e:
        where = (f"{SAFETY_PUSH_FAILED} ({str(e)[:200]}) — the fix exists "
                 "only in the worktree on this box")
    return f"{reason} — {where}" + (f":\n{detail}" if detail else "")


def _hang(e, what: str) -> tuple[str, str]:
    """(reason, detail) for a git command that hung, for _park_on_branch.

    A hang is not a failure with a message, so say plainly which command
    stopped answering and for how long; _failure_output supplies whatever
    partial output the timeout carried."""
    full, footer = _failure_output(e)
    reason = f"{what} timed out after {e.timeout}s"
    return reason, (full[-800:].strip() + footer) if full.strip() else ""


def _abort_rebase(wt: Path) -> None:
    """Best-effort cleanup after a rebase went wrong.

    check=False forgives a non-zero exit, not a hang: a rebase that timed out
    can leave a git that hangs its abort too, and that must not escape the
    handler that is already reporting the first hang."""
    try:
        run(["git", "rebase", "--abort"], cwd=wt, check=False)
    except (CmdError, OSError):
        pass


def push_worktree_to_dev(project, wt: Path,
                         branch: str) -> tuple[bool, str]:
    """Land a finished worktree branch on dev, serialised via the clone lock.

    If dev moved (a parallel engineer landed first), rebase and re-run the
    tests before pushing — the deterministic gate applies to what actually
    lands, not what was built.

    Every path that gives up parks the commit on origin/<branch> first (see
    _park_on_branch); the returned error names where it went. That includes
    a git that hangs rather than fails, which parks with the hang named as a
    hang — left to the general handler it would read as a rejected push
    (#102, #108, #110). Each CmdTimeout clause below therefore has to stay
    ordered before the CmdError one it sits with: CmdTimeout is a CmdError,
    so the general clause would otherwise swallow it."""
    dev = project["dev_branch"]
    for _ in range(3):
        try:
            run(["git", "fetch", "origin"], cwd=wt)
            behind = run(["git", "rev-list", "--count",
                          f"HEAD..origin/{dev}"], cwd=wt).strip()
        except CmdTimeout as e:
            return False, _park_on_branch(
                project, wt, branch,
                *_hang(e, f"checking whether {dev} had moved"))
        if behind != "0":
            try:
                run(["git", "rebase", f"origin/{dev}"], cwd=wt)
            except CmdTimeout as e:      # before CmdError: it is one
                _abort_rebase(wt)
                return False, _park_on_branch(
                    project, wt, branch,
                    *_hang(e, f"the rebase onto moved {dev}"))
            except CmdError:
                _abort_rebase(wt)
                return False, _park_on_branch(
                    project, wt, branch,
                    f"rebase onto moved {dev} conflicted")
            ok, out = run_tests(project, cwd=wt, setup=False)
            if not ok:
                return False, _park_on_branch(
                    project, wt, branch,
                    f"tests failed after rebase onto moved {dev}",
                    out[-800:])
        try:
            with clone_lock(project):
                run(["git", "push", "origin", f"HEAD:{dev}"], cwd=wt)
            return True, ""
        except CmdTimeout as e:          # before CmdError: it is one
            # Not a rejection: going around again would only hang again, and
            # a push that never answered may or may not have landed.
            return False, _park_on_branch(
                project, wt, branch, *_hang(e, f"the push to {dev}"))
        except CmdError as e:
            if "rejected" in (e.err or "") or "fetch first" in (e.err or ""):
                continue  # dev moved again while we were testing; go around
            return False, _park_on_branch(project, wt, branch,
                                          f"push failed: {str(e)[:300]}")
    return False, _park_on_branch(project, wt, branch,
                                  f"could not land on {dev} after 3 attempts")


def reconcile_dev(project) -> str:
    """Keep dev from going stale behind main.

    Operators commit and release straight to main sometimes; every agent
    branch is cut from dev, so a stale dev means fixes built on old code and
    release PRs that re-propose what already shipped. If origin/dev is a
    strict ancestor of origin/main, fast-forward it (a pure pointer move —
    no content decision). Returns "" (nothing to do), "fast-forwarded", or
    "diverged" (both moved; needs a human merge — never guessed at here).
    """
    dev, main = project["dev_branch"], project["main_branch"]
    d = ensure_clone(project)
    run(["git", "fetch", "origin", "--prune"], cwd=d)
    behind = run(["git", "rev-list", "--count",
                  f"origin/{dev}..origin/{main}"], cwd=d).strip()
    ahead = run(["git", "rev-list", "--count",
                 f"origin/{main}..origin/{dev}"], cwd=d).strip()
    if behind == "0":
        return ""
    if ahead != "0":
        return "diverged"
    run(["git", "push", "origin", f"origin/{main}:refs/heads/{dev}"], cwd=d)
    run(["git", "fetch", "origin", "--prune"], cwd=d)
    return "fast-forwarded"


def pr_run_dir(project, number: int) -> Path:
    """The throwaway directory a PR's code is tested in."""
    return config.DATA_DIR / "pr-runs" / project["name"] / str(number)


def fetch_pr_branch(project, number: int, branch: str) -> Path:
    """Check out PR #number merged onto origin/<dev>, in a throwaway clone.

    The merge happens in harness's clone (it is the one with the remote and
    the credentials), but the contributor's code is then copied out to
    data/pr-runs/<project>/<number>/repo and tested there. A worktree would
    not do: it shares .git with the clone, and the test command a PR ships is
    attacker-controlled. --no-hardlinks so the run cannot reach back into the
    clone's object store, and hooks point at an empty directory.

    Returns the checkout; the caller must remove_pr_run() when done.
    """
    d = clean_checkout(project, project["dev_branch"])
    # Forced refspec: the local pr-N branch outlives the run, and a PR whose
    # head was rewritten (force-push, rebase, amend, squash) is no longer a
    # descendant of what was fetched last time. Unforced, git would refuse the
    # update as non-fast-forward and the PR would be stuck at this line for
    # good.
    run(["git", "fetch", "origin",
         f"+refs/pull/{number}/head:refs/heads/pr-{number}"], cwd=d)
    run(["git", "checkout", "-B", branch, f"origin/{project['dev_branch']}"], cwd=d)
    run(["git", "merge", "--no-edit", f"pr-{number}"], cwd=d)  # raises on conflict
    remove_pr_run(project, number)
    hooks = pr_run_dir(project, number) / "no-hooks"
    hooks.mkdir(parents=True)
    checkout = pr_run_dir(project, number) / "repo"
    run(["git", "clone", "--no-hardlinks", "-c", f"core.hooksPath={hooks}",
         str(d), str(checkout)], timeout=1200)
    return checkout


def remove_pr_run(project, number: int) -> None:
    """Delete a PR's throwaway checkout, venv and scratch home."""
    shutil.rmtree(pr_run_dir(project, number), ignore_errors=True)


# --- tests ------------------------------------------------------------------

def _sandbox_env(project, home: Path | None = None) -> dict[str, str]:
    """The environment project-supplied commands run in.

    setup_command and test_command are the project's own build hooks, so on a
    community PR they are contributor-controlled code. Harness's own
    environment holds the GitHub token and the Claude credentials, so none of
    it is inherited: this is an allowlist of what a build actually needs, with
    HOME and TMPDIR pointed at scratch space so ~/.config/gh/hosts.yml,
    ~/.claude and ~/.git-credentials are out of reach as well.

    Not a sandbox: the command still has the network and can write anywhere
    the harness user can. See SECURITY.md.
    """
    home = home or config.DATA_DIR / "sandbox" / project["name"]
    tmp = home / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "TERM": "dumb",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for passthrough in ("LANG", "LC_ALL", "TZ"):
        if passthrough in os.environ:
            env[passthrough] = os.environ[passthrough]
    return env


def ensure_test_env(project) -> None:
    """Build the shared venv + install deps once, before parallel test runs."""
    d = repo_dir(project)
    py = _venv_python(project)
    setup_cmd = project["setup_command"].strip()
    if not setup_cmd and (d / "requirements.txt").exists():
        setup_cmd = f"{py} -m pip install -q -r requirements.txt"
    if setup_cmd:
        try:
            run(["bash", "-c", f'PATH="{py.parent}:$PATH" {setup_cmd}'],
                cwd=d, timeout=1200, check=False, env=_sandbox_env(project))
        except CmdTimeout:
            # check=False already forgives a failed setup; a hung one is no
            # worse. The test runs that follow report the real damage.
            pass



def venv_dir(project) -> Path:
    """Where a project's virtualenv lives. Naming it, not building it."""
    return config.DATA_DIR / "venvs" / project["name"]


def _venv_python(project, vdir: Path | None = None) -> Path:
    """A per-project virtualenv so test deps don't pollute harness's own env."""
    vdir = vdir or venv_dir(project)
    py = vdir / "bin" / "python"
    if not py.exists():
        vdir.parent.mkdir(parents=True, exist_ok=True)
        venv.create(vdir, with_pip=True)
    return py


def run_pr_tests(project, number: int) -> tuple[bool, str]:
    """Run a contributor's suite in its own throwaway checkout.

    Everything the run touches — checkout, venv, HOME — lives under the PR's
    run directory and goes when it does, so a PR cannot poison the venv the
    fix flow shares.
    """
    return run_tests(project, cwd=pr_run_dir(project, number) / "repo",
                     scratch=pr_run_dir(project, number))


def run_tests(project, cwd: Path | None = None, setup: bool = True,
              scratch: Path | None = None) -> tuple[bool, str]:
    """Run setup (unless suppressed) + the project's test command.

    cwd defaults to the main clone; pass a worktree for isolated runs.
    Parallel worktree runs share the per-project venv — suppress setup for
    all but one to avoid concurrent pip installs racing. Pass scratch to give
    one run a private venv and HOME underneath it instead (see run_pr_tests).
    Returns (passed, combined output tail).
    """
    d = cwd or repo_dir(project)
    py = _venv_python(project, scratch / "venv" if scratch else None)
    env = _sandbox_env(project, scratch / "home" if scratch else None)
    env_prefix = str(py.parent)
    outputs = []
    setup_cmd = project["setup_command"].strip() if setup else ""
    if setup and not setup_cmd and (d / "requirements.txt").exists():
        setup_cmd = f"{py} -m pip install -q -r requirements.txt"
    try:
        if setup_cmd:
            outputs.append(run(["bash", "-c",
                                f'PATH="{env_prefix}:$PATH" {setup_cmd}'],
                               cwd=d, timeout=1200, env=env))
        out = run(["bash", "-c",
                   f'PATH="{env_prefix}:$PATH" {project["test_command"]}'],
                  cwd=d, timeout=1800, env=env)
        outputs.append(out)
        tail = "\n".join(outputs)[-8000:]
        return True, tail
    except CmdError as e:
        # A hung suite is a failed suite, not a crashed cycle: every caller
        # (review, release, merge) reads the verdict, and an exception here
        # takes the whole cycle down instead. See #102. A hang arrives here
        # as CmdTimeout (a CmdError), and stays distinguishable from an
        # ordinary failure in the output _failure_output builds below.
        full, footer = _failure_output(e)
        # Surface the pytest verdict first: a FAILED line must never be
        # buried under thousands of deprecation warnings.
        marker = full.rfind("short test summary info")
        if marker != -1:
            tail = full[marker:][:3000] + "\n---\n" + full[-3000:]
        else:
            tail = full[-8000:]
        return False, tail + footer


def _failure_output(e) -> tuple[str, str]:
    """(combined output, footer) for a failed or timed-out command.

    Both carry .out/.err; a CmdTimeout also carries the limit it blew, and
    has to be tested for first because it is a CmdError. The footer names
    that limit, and goes last because every caller reads this tail from the
    end: a note at the top would be the first thing trimmed, and a partial
    run would then read as a plain failure.
    """
    combined = (e.out or "") + "\n" + (e.err or "")
    if isinstance(e, CmdTimeout):
        return combined, (f"\n--- the command timed out after {e.timeout}s; "
                          "any output above is partial ---")
    return combined, ""


# --- preview ----------------------------------------------------------------

# Where render.py writes: a fixed path in the worktree, so the engineer, the
# thread and a human all know where to look. Kept out of the commit, because
# wt_commit_all is `git add -A` and a screenshot is evidence for the run, not
# a file the project asked for.
SCREENSHOT_DIR = ".harness/screenshots"
_EXCLUDE_LINE = ".harness/"
RENDER_SCRIPT = Path(__file__).resolve().parent / "render.py"


def _project_field(project, key: str) -> str:
    """A project column that may not be there: an old row, or a test's dict."""
    try:
        return (project[key] or "").strip() if project else ""
    except (KeyError, IndexError, TypeError):
        return ""


def ensure_screenshot_dir(project, wt: Path) -> Path | None:
    """Make the worktree's screenshot directory, excluded from commits.

    The exclusion is written to the clone's own info/exclude — harness's
    clone, never the user's working copy — so the project's .gitignore is
    left alone and untracked PNGs never show up in `git status`, where they
    would read as work the engineer did. Returns None if that could not be
    written; the commit itself is guarded separately (wt_commit_all), so a
    failure here costs an honest `git status`, not a clean repository.
    """
    wt = Path(wt)
    try:
        common = run(["git", "rev-parse", "--git-common-dir"], cwd=wt).strip()
        base = Path(common) if Path(common).is_absolute() else wt / common
        info = base / "info"
        info.mkdir(parents=True, exist_ok=True)
        exclude = info / "exclude"
        existing = exclude.read_text() if exclude.exists() else ""
        if _EXCLUDE_LINE not in existing.split():
            with exclude.open("a") as fh:
                fh.write(f"\n# harness: render.py screenshots\n"
                         f"{_EXCLUDE_LINE}\n")
        d = wt / SCREENSHOT_DIR
        d.mkdir(parents=True, exist_ok=True)
        return d
    except (CmdError, OSError) as e:
        _say(f"Could not hide {SCREENSHOT_DIR} from git status ({e}); the "
             "commit drops it anyway", "warn", _project_field(project, "name"))
        return None


def render_command(project) -> str:
    """The command that renders this project's pages, or '' if it has no UI.

    An empty preview_command means there is nothing to render, and the
    engineer is told nothing about screenshots. The interpreter is harness's
    own, because that is the one with Playwright; the project's venv goes
    first on PATH so the app itself starts with its own dependencies.
    """
    preview = _project_field(project, "preview_command")
    if not preview:
        return ""
    return (f"{shlex.quote(sys.executable)} {shlex.quote(str(RENDER_SCRIPT))}"
            f" --command {shlex.quote(preview)}"
            f" --path-prefix {shlex.quote(str(venv_dir(project) / 'bin'))}"
            f" --out {SCREENSHOT_DIR}")


# --- version ----------------------------------------------------------------

def current_version(project) -> str:
    d = repo_dir(project)
    text = (d / project["version_file"]).read_text()
    m = re.search(project["version_pattern"], text)
    if not m:
        raise RuntimeError(
            f"version pattern not found in {project['version_file']}")
    return m.group("version")
