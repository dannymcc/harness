"""Harness configuration.

Global settings live here (env-overridable). Per-project settings (repo,
branches, policies) live in the database — see db.py — because harness manages
many harnesses and they are edited from the GUI.
"""
import os
from pathlib import Path

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
MAX_TURNS = int(os.environ.get("HARNESS_MAX_TURNS", "80"))
MAX_BUDGET_USD_PER_RUN = float(os.environ.get("HARNESS_MAX_BUDGET_USD_PER_RUN", "5.0"))

# --- Worker -----------------------------------------------------------------
POLL_INTERVAL_MINUTES = int(os.environ.get("HARNESS_POLL_INTERVAL_MINUTES", "30"))
ADMIN_INTERVAL_MINUTES = int(os.environ.get("HARNESS_ADMIN_INTERVAL_MINUTES", "60"))

# --- Web --------------------------------------------------------------------
BIND_HOST = os.environ.get("HARNESS_BIND_HOST", "0.0.0.0")
BIND_PORT = int(os.environ.get("HARNESS_BIND_PORT", "8300"))

# --- Agent personas (Spooks) ------------------------------------------------
# Harry runs the section. Each harness gets a senior officer as team lead;
# the ICs are the specialists.
CTO_NAME = "Harry"
ADMIN_NAME = "Tariq"      # housekeeping: prunes state, compacts history
LEAD_ROSTER = ["Tom", "Adam", "Ros", "Lucas", "Zoe", "Jo", "Danny", "Fiona"]
IC_NAMES = {
    "triage": "Ruth",     # analysis
    "review": "Ruth",
    "fix": "Malcolm",     # technical
    "release": "Colin",   # ops
    "security": "Zaf",    # security reviews (manually triggered)
}

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
    "fix_issues": "auto",
    # Merge community PRs (always validated + tested locally first).
    "merge_prs": "approve",
    # Dependabot dependency bumps (tested locally before merge).
    "merge_dependabot": "approve",
    # Post drafted comments/reviews publicly on GitHub.
    "post_comments": "approve",
    # Cut a release (dev -> main PR, merge, tag).
    "cut_release": "approve",
    # Releases are batched: propose when this many changes are queued...
    "release_min_changes": "3",
    # ...or when the oldest queued change is this old (days), whichever first.
    "release_max_age_days": "7",
}
