"""Tariq's desk: hourly housekeeping to keep token usage (and disk) down.

Two layers:
1. Deterministic pruning — free. Old events and runs are folded into
   aggregates, stale diffs/session ids are cleared from finished items, old
   transcript logs and SDK session files are deleted. Runs even while agent
   work is paused for API limits.
2. Rolling "desk notes" per project, maintained by Tariq on the cheap admin
   model. The notes stand in for raw history in the team lead and CTO
   prompts — that substitution is where the token saving actually happens.
   Only runs when enough new activity has accumulated, so a quiet harness
   costs nothing to keep tidy.
"""
import time
from pathlib import Path

from . import agents, config, db
from .agents import AgentStalled

EVENT_KEEP = 300          # newest events kept verbatim
RUN_KEEP = 200            # newest run rows kept; older fold into aggregates
REPORT_KEEP = 5           # newest report rows kept per (scope, project)
LOG_KEEP_DAYS = 14        # per-run transcript files
SESSION_KEEP_DAYS = 7     # Agent SDK session files (~/.claude/projects)
NOTES_MIN_NEW_EVENTS = 15 # don't wake Tariq for less than this


def due() -> bool:
    last = db.get_setting("last_admin_at")
    if not last:
        return True
    from datetime import datetime, timezone
    dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return age >= config.ADMIN_INTERVAL_MINUTES * 60


# --- deterministic layer ----------------------------------------------------

def _prune_events(c) -> int:
    row = c.execute(
        "SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?",
        (EVENT_KEEP,)).fetchone()
    if not row:
        return 0
    n = c.execute("SELECT COUNT(*) n FROM events WHERE id <= ?",
                  (row["id"],)).fetchone()["n"]
    c.execute("DELETE FROM events WHERE id <= ?", (row["id"],))
    return n


def _prune_runs(c) -> int:
    row = c.execute(
        "SELECT id FROM runs ORDER BY id DESC LIMIT 1 OFFSET ?",
        (RUN_KEEP,)).fetchone()
    if not row:
        return 0
    old = c.execute(
        "SELECT project, COALESCE(SUM(cost_usd),0) cost, COUNT(*) n "
        "FROM runs WHERE id <= ? GROUP BY project", (row["id"],)).fetchall()
    for r in old:
        key = f"archived_cost.{r['project']}"
        prev = c.execute("SELECT value FROM settings WHERE key = ?",
                         (key,)).fetchone()
        total = float(prev["value"] if prev else 0) + r["cost"]
        c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                  (key, str(total)))
    c.execute("DELETE FROM runs WHERE id <= ?", (row["id"],))
    return sum(r["n"] for r in old)


def _prune_reports(c) -> int:
    """Bound the reports table per (scope, project).

    Every persona memory note, lead summary, stand-up line and desk-notes
    rewrite inserts a fresh row, and readers only ever ask for the newest one
    per scope. A handful of older rows is enough to look back on; the rest is
    dead weight that `latest_report` pays for on every page load.
    """
    n = 0
    scopes = c.execute("SELECT DISTINCT scope, project FROM reports").fetchall()
    for s in scopes:
        row = c.execute(
            "SELECT id FROM reports WHERE scope = ? AND project = ? "
            "ORDER BY id DESC LIMIT 1 OFFSET ?",
            (s["scope"], s["project"], REPORT_KEEP)).fetchone()
        if not row:
            continue
        n += c.execute(
            "DELETE FROM reports WHERE scope = ? AND project = ? AND id <= ?",
            (s["scope"], s["project"], row["id"])).rowcount
    return n


# Statuses an item never comes back from: nothing will be resumed, so the
# working state behind it (diffs, session ids, worktrees) is fair game.
TERMINAL_STATUSES = ("released", "closed", "rejected")


def _trim_finished_items(c) -> int:
    qs = ",".join("?" * len(TERMINAL_STATUSES))
    cur = c.execute(
        "UPDATE items SET diff = '', session_id = '' "
        f"WHERE status IN ({qs}) "
        "AND (diff != '' OR session_id != '')", TERMINAL_STATUSES)
    return cur.rowcount


def _prune_files(root: Path, days: int) -> int:
    if not root.exists():
        return 0
    cutoff = time.time() - days * 86400
    n = 0
    for f in root.rglob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n


# Shorter than pipeline.STUCK_WORKING_HOURS (6) on purpose: the run is
# closed first, so the dashboard stops claiming an agent is still working,
# while the item stays `working` a while longer as headroom in case the
# session is merely slow. pipeline._unstick_working requeues the item once
# it has not moved for six hours.
ORPHAN_RUN_HOURS = 3


def _close_orphaned_runs(c) -> int:
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ORPHAN_RUN_HOURS)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    return c.execute(
        "UPDATE runs SET ok = 0, finished_at = ?, summary = ? "
        "WHERE finished_at IS NULL AND started_at < ?",
        (db.now(), db.HOUSEKEEPING_ORPHAN_SUMMARY, cutoff)).rowcount


WORKTREE_KEEP_DAYS = 3


def _live_branch_dirs(project: str) -> set[str]:
    """Worktree directory names an item may still be resumed into.

    An item that is not in a terminal state can be picked up again at any
    moment, and one held for an operator or Harry answer sits idle for
    exactly as long as the answer takes — so its worktree's mtime says
    nothing about whether the work is wanted. Keyed off item status rather
    than repo._worktree_is_live: the sweep then needs no clone lock and no
    git call per directory, and whether a surviving tree has the right
    branch on HEAD is the resume path's business, not housekeeping's.
    """
    qs = ",".join("?" * len(TERMINAL_STATUSES))
    with db.conn() as c:
        rows = c.execute(
            "SELECT branch FROM items WHERE project = ? AND branch != '' "
            f"AND status NOT IN ({qs})",
            (project, *TERMINAL_STATUSES)).fetchall()
    return {r["branch"].replace("/", "-") for r in rows}


def _prune_worktrees() -> tuple[int, int]:
    """Returns (removed, kept): kept counts stale worktrees still spoken for."""
    import shutil
    from .gh import run, CmdError
    n = kept = 0
    cutoff = time.time() - WORKTREE_KEEP_DAYS * 86400
    for p in db.all_projects():
        base = config.DATA_DIR / "worktrees" / p["name"]
        if not base.exists():
            continue
        live = _live_branch_dirs(p["name"])
        for wt in base.iterdir():
            try:
                if wt.is_dir() and wt.stat().st_mtime < cutoff:
                    if wt.name in live:
                        kept += 1
                        continue
                    shutil.rmtree(wt, ignore_errors=True)
                    n += 1
            except OSError:
                pass
        clone = config.REPOS_DIR / p["name"]
        if (clone / ".git").exists():
            try:
                # Bounded: an hourly sweep must not be able to sit on a
                # wedged git forever. check=False forgives a failed prune;
                # a hung one comes back as CmdTimeout, a CmdError (#110).
                run(["git", "worktree", "prune"], cwd=clone, check=False,
                    timeout=60)
            except CmdError as e:
                db.log_event(f"git worktree prune did not finish: "
                             f"{str(e)[:150]}", "warn", project=p["name"])
    return n, kept


PR_RUN_KEEP_HOURS = 12


def _prune_pr_runs() -> int:
    """Sweep up throwaway PR checkouts that a crash left behind.

    The review flow removes its own run directory; this is only for the case
    where the process died between fetching a PR and finishing with it.
    """
    import shutil
    n = 0
    cutoff = time.time() - PR_RUN_KEEP_HOURS * 3600
    for p in db.all_projects():
        base = config.DATA_DIR / "pr-runs" / p["name"]
        if not base.exists():
            continue
        for d in base.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    n += 1
            except OSError:
                pass
    return n


def prune() -> str:
    with db.conn() as c:
        ev = _prune_events(c)
        rn = _prune_runs(c)
        rp = _prune_reports(c)
        it = _trim_finished_items(c)
        orph = _close_orphaned_runs(c)
    logs = _prune_files(config.LOG_DIR, LOG_KEEP_DAYS)
    wts, wts_kept = _prune_worktrees()
    prs = _prune_pr_runs()
    sessions = _prune_sdk_sessions()
    parts = []
    if ev: parts.append(f"{ev} events folded")
    if rn: parts.append(f"{rn} runs archived")
    if rp: parts.append(f"{rp} old reports removed")
    if it: parts.append(f"{it} finished items trimmed")
    if orph: parts.append(f"{orph} orphaned runs closed")
    if logs: parts.append(f"{logs} old logs removed")
    if wts: parts.append(f"{wts} stale worktrees removed")
    if wts_kept: parts.append(f"{wts_kept} stale worktrees kept (in play)")
    if prs: parts.append(f"{prs} stale PR runs removed")
    if sessions: parts.append(f"{sessions} stale sessions removed")
    return ", ".join(parts)


def _prune_sdk_sessions() -> int:
    """Delete old Agent SDK session transcripts for the harness's own repos.

    Strictly scoped: Claude Code stores sessions under
    ~/.claude/projects/<encoded-cwd>/, and harness agents only ever run with
    cwd inside REPOS_DIR or the throwaway PR checkouts under pr-runs — so only
    directories whose encoded name matches one of those are touched, only
    *.jsonl files inside them, and never anything under a memory/ path.
    Everything else in ~/.claude belongs to the human.
    """
    root = Path.home() / ".claude" / "projects"
    if not root.exists():
        return 0
    prefixes = tuple(str(p).replace("/", "-").replace(".", "-")
                     for p in (config.REPOS_DIR, config.DATA_DIR / "pr-runs"))
    cutoff = time.time() - SESSION_KEEP_DAYS * 86400
    n = 0
    for d in root.iterdir():
        if not d.is_dir() or not d.name.startswith(prefixes):
            continue
        for f in d.rglob("*.jsonl"):
            if "memory" in f.parts:
                continue
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    n += 1
            except OSError:
                pass
    return n


# --- desk notes layer -------------------------------------------------------

async def update_desk_notes() -> None:
    for p in db.all_projects(enabled_only=True):
        name = p["name"]
        marker_key = f"notes_event_id.{name}"
        last_seen = int(db.get_setting(marker_key, "0"))
        with db.conn() as c:
            new = c.execute(
                "SELECT * FROM events WHERE project = ? AND id > ? "
                "ORDER BY id LIMIT 200", (name, last_seen)).fetchall()
        if len(new) < NOTES_MIN_NEW_EVENTS:
            continue
        old = db.latest_report("notes", name)
        events_txt = "\n".join(f"[{e['level']}] {e['ts']} {e['message']}"
                               for e in new)[:12000]
        try:
            res = await agents.compact_notes(name, old["content"] if old else "",
                                             events_txt)
        except AgentStalled:
            return
        if res["ok"]:
            db.save_report("notes", name, res["output"]["notes_markdown"])
            db.set_setting(marker_key, str(new[-1]["id"]))


MEMORY_CONDENSE_AT = 2000  # chars

MEMORY_KEYS = ("analyst", "engineering", "lead", "ops", "security")


async def condense_memories() -> None:
    for p in db.all_projects(enabled_only=True):
        for key in MEMORY_KEYS:
            mem = db.persona_memory(p["name"], key)
            if len(mem) < MEMORY_CONDENSE_AT:
                continue
            try:
                res = await agents.compact_memory(p["name"], key, mem)
            except AgentStalled:
                return
            if res["ok"]:
                db.save_report(f"memory:{key}", p["name"],
                               res["output"]["notes_markdown"])


async def run(allow_agent: bool) -> None:
    summary = prune()
    if summary:
        db.log_event(f"Tariq: {summary}")
    if allow_agent:
        await update_desk_notes()
        await condense_memories()
    db.set_setting("last_admin_at", db.now())
