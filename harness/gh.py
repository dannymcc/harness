"""Thin wrappers around the gh CLI and git.

All GitHub access goes through gh (authenticated via GH_TOKEN or gh auth
login); all git access goes through harness's own clone of each project.

Merges never bypass branch protection: if required checks are pending or
failing, `gh pr merge` fails and the item stays blocked for a human.
"""
import json
import subprocess
from pathlib import Path


class CmdError(RuntimeError):
    def __init__(self, cmd: list[str], code: int | None, out: str, err: str,
                 message: str | None = None):
        self.cmd, self.code, self.out, self.err = cmd, code, out, err
        super().__init__(message or
                         f"{' '.join(cmd)} -> {code}: {err.strip() or out.strip()}")


class CmdTimeout(CmdError):
    """A command that stopped answering, rather than one that failed.

    A subclass of CmdError so that every `except CmdError` covers a hang by
    default — the safe direction is to park and report, not to take the
    cycle down at whichever call site forgot the second exception (#110).
    Callers that word a hang differently test `isinstance(e, CmdTimeout)`
    and read `.timeout`; `.code` is None because the command never exited.
    """
    def __init__(self, cmd: list[str], timeout: int | float,
                 out: str = "", err: str = ""):
        self.timeout = timeout
        super().__init__(cmd, None, out, err,
                         f"{' '.join(cmd)} -> timed out after {timeout}s")


def _text(out) -> str:
    """Whatever a command left behind, as str: TimeoutExpired carries bytes
    or str depending on how the command was run."""
    if out is None:
        return ""
    return out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)


def run(cmd: list[str], cwd: Path | None = None, check: bool = True,
        timeout: int = 600, env: dict | None = None) -> str:
    """Run a command and return stdout.

    env=None inherits harness's own environment — right for git and gh, which
    need the token. Pass an explicit env for anything project-supplied: see
    repo._sandbox_env.

    A hang comes back as CmdTimeout, not subprocess.TimeoutExpired: check
    forgives a non-zero exit, never a command that never answered.
    """
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        raise CmdTimeout(cmd, e.timeout, _text(e.output),
                         _text(e.stderr)) from e
    if check and p.returncode != 0:
        raise CmdError(cmd, p.returncode, p.stdout, p.stderr)
    return p.stdout


def gh_json(repo: str, args: list[str]) -> list | dict:
    out = run(["gh", *args, "-R", repo])
    return json.loads(out) if out.strip() else []


# --- listing ----------------------------------------------------------------

def list_issues(repo: str) -> list[dict]:
    return gh_json(repo, [
        "issue", "list", "--state", "open", "--limit", "100",
        "--json", "number,title,author,updatedAt",
    ])


def list_prs(repo: str) -> list[dict]:
    return gh_json(repo, [
        "pr", "list", "--state", "open", "--limit", "100",
        "--json", "number,title,author,updatedAt,isDraft",
    ])


def issue_detail(repo: str, number: int) -> dict:
    return gh_json(repo, [
        "issue", "view", str(number),
        "--json", "number,title,body,author,labels,comments,state,createdAt",
    ])


def pr_detail(repo: str, number: int) -> dict:
    return gh_json(repo, [
        "pr", "view", str(number),
        "--json", ("number,title,body,author,baseRefName,headRefName,state,"
                   "isDraft,mergeable,additions,deletions,changedFiles,"
                   "comments,reviews,statusCheckRollup,createdAt"),
    ])


# Conclusions GitHub reports for a run that did not actually go red.
# Anything else — failure, timed_out, cancelled, startup_failure,
# action_required — is a build that did not produce what it usually does.
CI_OK_CONCLUSIONS = ("success", "skipped", "neutral")


def commit_ci(repo: str, sha: str) -> dict:
    """What GitHub Actions makes of one commit, as a single verdict.

    Returns {"state", "conclusion", "url"} where state is:

    - ``none``    — GitHub has no workflow run for the commit (yet, or at all)
    - ``pending`` — at least one run is queued or still going
    - ``done``    — every run has finished; ``conclusion`` is ``success``, or
      the first non-success conclusion, and ``url`` points at that run.

    Read-only, and wrapped like every other call here: a hang comes back as
    CmdTimeout, a gh failure as CmdError, so a caller that catches CmdError
    covers both.
    """
    runs = gh_json(repo, ["run", "list", "--commit", sha, "--limit", "20",
                          "--json", "databaseId,name,status,conclusion,url"])
    if not runs:
        return {"state": "none", "conclusion": "", "url": ""}
    first_url = runs[0].get("url") or ""
    if any(r.get("status") != "completed" for r in runs):
        return {"state": "pending", "conclusion": "", "url": first_url}
    for r in runs:
        if r.get("conclusion") not in CI_OK_CONCLUSIONS:
            return {"state": "done",
                    "conclusion": r.get("conclusion") or "failure",
                    "url": r.get("url") or ""}
    return {"state": "done", "conclusion": "success", "url": first_url}


def pr_diff(repo: str, number: int, max_chars: int = 200_000) -> str:
    out = run(["gh", "pr", "diff", str(number), "-R", repo])
    if len(out) > max_chars:
        out = out[:max_chars] + "\n... [diff truncated] ..."
    return out


# --- acting -----------------------------------------------------------------

def comment_issue(repo: str, number: int, body: str) -> None:
    run(["gh", "issue", "comment", str(number), "-R", repo, "--body", body])


def comment_pr(repo: str, number: int, body: str) -> None:
    run(["gh", "pr", "comment", str(number), "-R", repo, "--body", body])


def create_issue(repo: str, title: str, body: str) -> int:
    out = run(["gh", "issue", "create", "-R", repo,
               "--title", title, "--body", body])
    return int(out.strip().rstrip("/").rsplit("/", 1)[-1])


def close_issue(repo: str, number: int, comment: str = "") -> None:
    args = ["gh", "issue", "close", str(number), "-R", repo]
    if comment:
        args += ["--comment", comment]
    run(args)


def retarget_pr(repo: str, number: int, base: str) -> None:
    run(["gh", "pr", "edit", str(number), "-R", repo, "--base", base])


def merge_pr(repo: str, number: int, squash: bool = True) -> None:
    args = ["gh", "pr", "merge", str(number), "-R", repo]
    args.append("--squash" if squash else "--merge")
    run(args)


def close_pr(repo: str, number: int, comment: str = "") -> None:
    if comment:
        comment_pr(repo, number, comment)
    run(["gh", "pr", "close", str(number), "-R", repo])


def publish_release(repo: str, tag: str, title: str, notes: str) -> None:
    run(["gh", "release", "create", tag, "-R", repo, "--verify-tag",
         "--title", title, "--notes", notes])


def create_pr(repo: str, base: str, head: str, title: str, body: str) -> int:
    out = run(["gh", "pr", "create", "-R", repo, "--base", base, "--head", head,
               "--title", title, "--body", body])
    # gh prints the PR URL; the number is the last path segment
    return int(out.strip().rstrip("/").rsplit("/", 1)[-1])
