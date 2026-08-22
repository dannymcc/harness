"""Manage harness's clone of each project and run its test suite there.

The clone under data/repos/<project> belongs to harness: agents edit it, the
pipeline resets it. It is never the user's own working copy.
"""
import re
import venv
from pathlib import Path

from . import config
from .gh import run, CmdError


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


def commit_all(project, message: str) -> None:
    d = repo_dir(project)
    run(["git", "add", "-A"], cwd=d)
    run(["git", "commit", "-m", message], cwd=d)


def push_branch_to(project, local_branch: str, remote_branch: str) -> None:
    d = repo_dir(project)
    run(["git", "push", "origin", f"{local_branch}:{remote_branch}"], cwd=d)


def fetch_pr_branch(project, number: int, branch: str) -> Path:
    """Check out PR #number merged onto origin/<dev> as local branch."""
    d = clean_checkout(project, project["dev_branch"])
    run(["git", "fetch", "origin", f"pull/{number}/head:pr-{number}"], cwd=d)
    run(["git", "checkout", "-B", branch, f"origin/{project['dev_branch']}"], cwd=d)
    run(["git", "merge", "--no-edit", f"pr-{number}"], cwd=d)  # raises on conflict
    return d


# --- tests ------------------------------------------------------------------

def _venv_python(project) -> Path:
    """A per-project virtualenv so test deps don't pollute harness's own env."""
    vdir = config.DATA_DIR / "venvs" / project["name"]
    py = vdir / "bin" / "python"
    if not py.exists():
        vdir.parent.mkdir(parents=True, exist_ok=True)
        venv.create(vdir, with_pip=True)
    return py


def run_tests(project) -> tuple[bool, str]:
    """Run setup (if any) + the project's test command in its clone.

    Returns (passed, combined output tail).
    """
    d = repo_dir(project)
    py = _venv_python(project)
    env_prefix = str(py.parent)
    outputs = []
    setup = project["setup_command"].strip()
    if not setup and (d / "requirements.txt").exists():
        setup = f"{py} -m pip install -q -r requirements.txt"
    try:
        if setup:
            outputs.append(run(["bash", "-c", f'PATH="{env_prefix}:$PATH" {setup}'],
                               cwd=d, timeout=1200))
        out = run(["bash", "-c",
                   f'PATH="{env_prefix}:$PATH" {project["test_command"]}'],
                  cwd=d, timeout=1800)
        outputs.append(out)
        tail = "\n".join(outputs)[-8000:]
        return True, tail
    except CmdError as e:
        tail = ((e.out or "") + "\n" + (e.err or ""))[-8000:]
        return False, tail


# --- version ----------------------------------------------------------------

def current_version(project) -> str:
    d = repo_dir(project)
    text = (d / project["version_file"]).read_text()
    m = re.search(project["version_pattern"], text)
    if not m:
        raise RuntimeError(
            f"version pattern not found in {project['version_file']}")
    return m.group("version")
