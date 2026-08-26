"""Harness configuration.

Global settings live here (env-overridable). Per-project settings (repo,
branches, policies) live in the database — see db.py — because harness manages
many harnesses and they are edited from the GUI.
"""
import os
import re
import subprocess
from pathlib import Path

# The single source of truth for the number: the release process bumps this
# line (version file `harness/config.py`, pattern `VERSION\s*=\s*"..."`) and
# tags the commit. Don't edit it by hand.
VERSION = "0.21.3"

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_STAMP_FILE = Path(__file__).resolve().parent / "_build_sha"


def _clean(sha: str) -> str:
    """Accept a plausible git SHA, truncated to 7; anything else is ''."""
    sha = (sha or "").strip().lower()
    return sha[:7] if _SHA_RE.match(sha) else ""


def _git_head_sha() -> str:
    """HEAD of the checkout harness is running from, or '' if it can't tell.

    Fully guarded: a missing git binary, a slow disk, or a non-zero exit
    ("dubious ownership" and friends) all mean "unknown", never an exception.
    """
    root = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--short=7", "HEAD"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return _clean(out.stdout) if out.returncode == 0 else ""


def _stamp_sha() -> str:
    """The SHA written into the image at build time, if there is one."""
    try:
        return _clean(_STAMP_FILE.read_text())
    except OSError:
        return ""


def _build_sha() -> str:
    """Identify the running build: env, then build stamp, then the checkout.

    This runs once at import and config is imported everywhere, so nothing
    here may raise or hang.
    """
    return (_clean(os.environ.get("HARNESS_GIT_SHA", ""))
            or _stamp_sha()
            or _git_head_sha())


def _display(sha: str) -> str:
    """Say "unknown build" out loud rather than showing a bare version: a
    footer the operator can't tie to a commit is worse than one that admits
    it can't."""
    return f"v{VERSION} ({sha})" if sha else f"v{VERSION} (unknown build)"


GIT_SHA = _build_sha()
DISPLAY_VERSION = _display(GIT_SHA)

# --- Paths ------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("HARNESS_DATA_DIR", "./data")).resolve()
REPOS_DIR = DATA_DIR / "repos"        # harness's own clones, one per project
DB_PATH = DATA_DIR / "harness.db"
LOG_DIR = DATA_DIR / "logs"           # per-run agent transcripts

# --- Claude -----------------------------------------------------------------
# The Agent SDK inherits credentials from the environment
# (ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN via `claude setup-token`).
MODEL = os.environ.get("HARNESS_MODEL", "claude-opus-5")
# Housekeeping/compaction runs are summarisation, not engineering — a cheap
# model keeps the housekeeper from eating the budget it exists to protect.
ADMIN_MODEL = os.environ.get("HARNESS_ADMIN_MODEL", "claude-haiku-4-5")
# Triage and PR review are read-and-judge: is it valid, what is the plan,
# write the reproduction test. A mid-tier model does this well at a
# fraction of the cost; the repro test is proven against the code either
# way, and the engineer (on MODEL) gets the final say. Set to MODEL's
# value to keep everything on one model.
TRIAGE_MODEL = os.environ.get("HARNESS_TRIAGE_MODEL", "claude-sonnet-5")
MAX_TURNS = int(os.environ.get("HARNESS_MAX_TURNS", "80"))
MAX_BUDGET_USD_PER_RUN = float(os.environ.get("HARNESS_MAX_BUDGET_USD_PER_RUN", "5.0"))

# --- Worker -----------------------------------------------------------------
# How often each desk syncs with GitHub when idle. Agent work is event-driven
# (new items, triage results, directives) so a short interval is cheap: a
# sync is two gh calls, and a wake with nothing new runs no agents.
POLL_INTERVAL_MINUTES = int(os.environ.get("HARNESS_POLL_INTERVAL_MINUTES", "5"))
ADMIN_INTERVAL_MINUTES = int(os.environ.get("HARNESS_ADMIN_INTERVAL_MINUTES", "60"))
# On SIGTERM (deploy, watchtower, compose down) the worker stops starting
# new agent runs and the process waits this long for in-flight ones to
# finish before exiting. Pair it with a matching stop_grace_period in the
# compose file, or Docker SIGKILLs the container after 10s regardless.
DRAIN_TIMEOUT_S = int(os.environ.get("HARNESS_DRAIN_TIMEOUT_S", str(25 * 60)))

# --- Notifications (ntfy) ---------------------------------------------------
# Disabled unless a topic is set. On the public ntfy.sh server the topic
# name is effectively a password — pick something unguessable.
NTFY_URL = os.environ.get("HARNESS_NTFY_URL", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("HARNESS_NTFY_TOPIC", "")
# Base URL used for tap-through links in notifications.
PUBLIC_URL = os.environ.get("HARNESS_PUBLIC_URL", "")

# --- Web --------------------------------------------------------------------
BIND_HOST = os.environ.get("HARNESS_BIND_HOST", "0.0.0.0")
BIND_PORT = int(os.environ.get("HARNESS_BIND_PORT", "8300"))

# --- Operator ----------------------------------------------------------------
# The human the section answers to. Escalations, approvals and directions
# are addressed to this name throughout the GUI and agent prompts.
OPERATOR = os.environ.get("HARNESS_OPERATOR_NAME", "the maintainer")

# --- Agent personas (Spooks) ------------------------------------------------
# Harry runs the section. Each harness gets a senior officer as team lead;
# the ICs are the specialists.
CTO_NAME = "Harry"
ADMIN_NAME = "Tariq"      # housekeeping: prunes state, compacts history
LEAD_ROSTER = ["Tom", "Adam", "Ros", "Lucas", "Zoe", "Jo", "Danny", "Fiona"]
# Harry can hire extra engineers onto a busy desk (max 2 per desk) or stand
# down specialists that never see work. Benching is visibility only — benched
# agents still respond when triggered.
HIRE_POOL = ["Dimitri", "Beth", "Calum", "Erin", "Alec", "Will"]
MAX_EXTRA_ENGINEERS = 2
IC_NAMES = {
    "triage": "Ruth",     # analysis
    "review": "Ruth",
    "fix": "Malcolm",     # technical
    "release": "Colin",   # ops
    "security": "Zaf",    # security reviews (manually triggered)
}


def persona(role: str, task: str, lead_name: str = "") -> str:
    """Who a run belongs to, by role and task. One mapping for the GUI, the
    run pages and the composer's /tell, so a hire or a rename lands once."""
    if role == "cto":
        return CTO_NAME
    if role == "admin":
        return ADMIN_NAME
    if role == "lead":
        return lead_name or "lead"
    return IC_NAMES.get(task, "IC")


# --- Per-project defaults ---------------------------------------------------
# "auto"    – harness acts on its own verdict
# "approve" – harness prepares the action and waits for a click in the GUI
PROJECT_DEFAULTS = {
    "dev_branch": "dev",
    "main_branch": "main",
    "version_file": "config.py",
    "version_pattern": r"APP_VERSION\s*=\s*'(?P<version>[^']+)'",
    "test_command": "python -m pytest -x -q",
    "setup_command": "",              # e.g. "pip install -r requirements.txt"
}

POLICY_DEFAULTS = {
    # Investigate valid bug reports, fix on a branch, run tests, push to dev.
    # "auto"    – Ruth's fixable verdict starts the fix straight away
    # "lead"    – the team lead's plan is the sign-off (the section decides;
    #             the operator's gate moves to the release)
    # "approve" – the operator clicks approve before an engineer starts
    "fix_issues": "auto",
    # Team leads may open tracking issues on the repo from their plan
    # (capped by how many of theirs are already open and unworked).
    # "off" disables it.
    "file_issues": "auto",
    # Merge community PRs (always validated + tested locally first).
    "merge_prs": "approve",
    # Dependabot dependency bumps (tested locally before merge).
    "merge_dependabot": "approve",
    # Post drafted comments/reviews publicly on GitHub.
    "post_comments": "approve",
    # Cut a release (dev -> main PR, merge, tag).
    "cut_release": "approve",
    # What sets a release off. "changes" — the two thresholds below, whichever
    # comes first. "daily"/"weekly"/"monthly" — one release a window at most,
    # timed from the last release, with the thresholds ignored.
    "release_schedule": "changes",
    # Releases are batched: propose when this many changes are queued...
    "release_min_changes": "3",
    # ...or when the oldest queued change is this old (days), whichever first.
    "release_max_age_days": "7",
    # Agent work runs only inside these local hours ("HH-HH", or "always").
    # Human-triggered actions (approvals, Run cycle now, answers) override.
    "active_hours": "always",
    # The desk stops starting agent work once it has spent this much in the
    # last 24h (USD). The governor that makes "run until the board is clear"
    # safe to leave unattended. Harry sees the hold at stand-up.
    "daily_budget_usd": "30",
}

TIMEZONE = os.environ.get("HARNESS_TZ", "Europe/London")
