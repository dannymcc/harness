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


def _trim_finished_items(c) -> int:
    cur = c.execute(
        "UPDATE items SET diff = '', session_id = '' "
        "WHERE status IN ('released', 'closed', 'rejected') "
        "AND (diff != '' OR session_id != '')")
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


ORPHAN_RUN_HOURS = 3


def _close_orphaned_runs(c) -> int:
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ORPHAN_RUN_HOURS)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    return c.execute(
        "UPDATE runs SET ok = 0, finished_at = ?, "
        "summary = 'orphaned (no result recorded)' "
        "WHERE finished_at IS NULL AND started_at < ?",
        (db.now(), cutoff)).rowcount


def prune() -> str:
    with db.conn() as c:
        ev = _prune_events(c)
        rn = _prune_runs(c)
        it = _trim_finished_items(c)
        orph = _close_orphaned_runs(c)
    logs = _prune_files(config.LOG_DIR, LOG_KEEP_DAYS)
    sessions = _prune_sdk_sessions()
    parts = []
    if ev: parts.append(f"{ev} events folded")
    if rn: parts.append(f"{rn} runs archived")
    if it: parts.append(f"{it} finished items trimmed")
    if orph: parts.append(f"{orph} orphaned runs closed")
    if logs: parts.append(f"{logs} old logs removed")
    if sessions: parts.append(f"{sessions} stale sessions removed")
    return ", ".join(parts)


def _prune_sdk_sessions() -> int:
    """Delete old Agent SDK session transcripts for the harness's own repos.

    Strictly scoped: Claude Code stores sessions under
    ~/.claude/projects/<encoded-cwd>/, and harness agents only ever run with
    cwd inside REPOS_DIR — so only directories whose encoded name matches
    REPOS_DIR are touched, only *.jsonl files inside them, and never anything
    under a memory/ path. Everything else in ~/.claude belongs to the human.
    """
    root = Path.home() / ".claude" / "projects"
    if not root.exists():
        return 0
    prefix = str(config.REPOS_DIR).replace("/", "-").replace(".", "-")
    cutoff = time.time() - SESSION_KEEP_DAYS * 86400
    n = 0
    for d in root.iterdir():
        if not d.is_dir() or not d.name.startswith(prefix):
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


async def run(allow_agent: bool) -> None:
    summary = prune()
    if summary:
        db.log_event(f"Tariq: {summary}")
    if allow_agent:
        await update_desk_notes()
    db.set_setting("last_admin_at", db.now())
