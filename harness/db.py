"""SQLite state store.

One connection per call keeps things simple and safe across the worker task
and web handlers (SQLite serialises writes; volumes are tiny here).

Everything except global settings is scoped to a project (one project = one
managed repository = one harness).
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,        -- slug, used in URLs and paths
    repo TEXT NOT NULL,               -- owner/name on GitHub
    dev_branch TEXT NOT NULL,
    main_branch TEXT NOT NULL,
    version_file TEXT NOT NULL,
    version_pattern TEXT NOT NULL,
    test_command TEXT NOT NULL,
    setup_command TEXT NOT NULL DEFAULT '',
    lead_name TEXT NOT NULL DEFAULT 'Tom',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    kind TEXT NOT NULL,               -- 'issue' | 'pr'
    number INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    gh_state TEXT NOT NULL DEFAULT 'open',
    gh_updated_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    -- new -> triaged -> approved -> working -> queued -> released
    --                -> waiting_human | blocked | rejected | closed
    verdict TEXT NOT NULL DEFAULT '',
    verdict_summary TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT '',
    draft_comment TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    diff TEXT NOT NULL DEFAULT '',
    commits TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    queued_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project, kind, number)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'ic',     -- 'ic' | 'lead' | 'cto'
    item_key TEXT NOT NULL DEFAULT '',   -- 'issue#123' / 'pr#45' / 'release' / ''
    task TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    ok INTEGER,
    cost_usd REAL NOT NULL DEFAULT 0,
    turns INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    log_path TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',  -- proposed | released | abandoned
    pr_number INTEGER,
    notes TEXT NOT NULL DEFAULT '',
    items_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    released_at TEXT
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,              -- 'cto' | 'lead'
    project TEXT NOT NULL DEFAULT '', -- empty for CTO reports
    content TEXT NOT NULL,            -- markdown
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',
    asked_by TEXT NOT NULL,
    item_key TEXT NOT NULL DEFAULT '',
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- open | answered | dismissed
    answer TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    answered_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


MIGRATIONS = [
    "ALTER TABLE runs ADD COLUMN agent TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE questions ADD COLUMN answered_by TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE questions ADD COLUMN options TEXT NOT NULL DEFAULT ''",
]


@contextmanager
def conn():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    for mig in MIGRATIONS:
        try:
            c.execute(mig)
        except sqlite3.OperationalError:
            pass  # already applied
    try:
        yield c
        c.commit()
    finally:
        c.close()


# --- projects ---------------------------------------------------------------

def create_project(name: str, repo: str, **overrides) -> None:
    vals = dict(config.PROJECT_DEFAULTS)
    vals.update({k: v for k, v in overrides.items() if v})
    with conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
        lead = config.LEAD_ROSTER[n % len(config.LEAD_ROSTER)]
        c.execute(
            """INSERT INTO projects (name, repo, dev_branch, main_branch,
                 version_file, version_pattern, test_command, setup_command,
                 lead_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, repo, vals["dev_branch"], vals["main_branch"],
             vals["version_file"], vals["version_pattern"],
             vals["test_command"], vals["setup_command"], lead, now()),
        )


def update_project(name: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE projects SET {cols} WHERE name = ?",
                  (*fields.values(), name))


def get_project(name: str):
    with conn() as c:
        return c.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()


def all_projects(enabled_only: bool = False):
    q = "SELECT * FROM projects"
    if enabled_only:
        q += " WHERE enabled = 1"
    with conn() as c:
        return c.execute(q + " ORDER BY name").fetchall()


# --- events -----------------------------------------------------------------

def log_event(message: str, level: str = "info", project: str = "") -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO events (project, ts, level, message) VALUES (?, ?, ?, ?)",
            (project, now(), level, message),
        )


def recent_events(limit: int = 50, project: str | None = None):
    with conn() as c:
        if project is None:
            return c.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return c.execute(
            "SELECT * FROM events WHERE project IN (?, '') ORDER BY id DESC LIMIT ?",
            (project, limit),
        ).fetchall()


# --- settings / policy ------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    with conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def policy(project: str, key: str) -> str:
    return get_setting(f"policy.{project}.{key}", config.POLICY_DEFAULTS[key])


def set_policy(project: str, key: str, value: str) -> None:
    set_setting(f"policy.{project}.{key}", value)


def all_policies(project: str) -> dict:
    return {k: policy(project, k) for k in config.POLICY_DEFAULTS}


# --- backoff (API limit stalls; account-wide, so global) --------------------

def pause_until(ts_iso: str, reason: str) -> None:
    set_setting("paused_until", ts_iso)
    set_setting("paused_reason", reason)
    log_event(f"Agent work paused until {ts_iso}: {reason}", "warn")
    from . import notify
    notify.send("Agent work paused", f"Until {ts_iso}: {reason[:200]}",
                tags="pause_button")


def paused_until() -> str | None:
    ts = get_setting("paused_until")
    if not ts:
        return None
    if ts <= now():  # ISO-8601 UTC strings compare chronologically
        set_setting("paused_until", "")
        set_setting("paused_reason", "")
        set_setting("backoff_count", "0")
        log_event("Usage-limit pause expired; resuming agent work")
        return None
    return ts


# --- items ------------------------------------------------------------------

def upsert_item(project: str, kind: str, number: int, title: str, author: str,
                gh_state: str, gh_updated_at: str) -> None:
    with conn() as c:
        c.execute(
            """INSERT INTO items (project, kind, number, title, author,
                                  gh_state, gh_updated_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(project, kind, number) DO UPDATE SET
                 title = excluded.title,
                 gh_state = excluded.gh_state,
                 gh_updated_at = excluded.gh_updated_at,
                 updated_at = excluded.updated_at""",
            (project, kind, number, title, author, gh_state, gh_updated_at,
             now(), now()),
        )


def update_item(project: str, kind: str, number: int, **fields) -> None:
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with conn() as c:
        c.execute(
            f"UPDATE items SET {cols} WHERE project = ? AND kind = ? AND number = ?",
            (*fields.values(), project, kind, number),
        )


def get_item(project: str, kind: str, number: int):
    with conn() as c:
        return c.execute(
            "SELECT * FROM items WHERE project = ? AND kind = ? AND number = ?",
            (project, kind, number),
        ).fetchone()


def items_by_status(project: str, *statuses: str):
    qs = ",".join("?" * len(statuses))
    with conn() as c:
        return c.execute(
            f"SELECT * FROM items WHERE project = ? AND status IN ({qs}) "
            "ORDER BY number",
            (project, *statuses),
        ).fetchall()


def project_items(project: str):
    with conn() as c:
        return c.execute(
            "SELECT * FROM items WHERE project = ? "
            "ORDER BY gh_state = 'open' DESC, number DESC",
            (project,),
        ).fetchall()


def counts_by_status(project: str) -> dict:
    with conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM items "
            "WHERE project = ? AND gh_state = 'open' GROUP BY status",
            (project,),
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}


# --- runs -------------------------------------------------------------------

def start_run(project: str, role: str, item_key: str, task: str, model: str,
              agent: str = "") -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO runs (project, role, item_key, task, model, agent, "
            "started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project, role, item_key, task, model, agent, now()),
        )
        return cur.lastrowid


def finish_run(run_id: int, ok: bool, cost_usd: float, turns: int,
               summary: str, log_path: str = "") -> None:
    with conn() as c:
        c.execute(
            """UPDATE runs SET ok = ?, cost_usd = ?, turns = ?, summary = ?,
                              log_path = ?, finished_at = ? WHERE id = ?""",
            (1 if ok else 0, cost_usd, turns, summary, log_path, now(), run_id),
        )


def recent_runs(limit: int = 30, project: str | None = None):
    with conn() as c:
        if project is None:
            return c.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return c.execute(
            "SELECT * FROM runs WHERE project = ? ORDER BY id DESC LIMIT ?",
            (project, limit),
        ).fetchall()


def total_cost(project: str | None = None) -> float:
    """Live run rows plus costs archived by housekeeping."""
    with conn() as c:
        if project is None:
            row = c.execute("SELECT COALESCE(SUM(cost_usd), 0) t FROM runs").fetchone()
            arch = c.execute(
                "SELECT COALESCE(SUM(CAST(value AS REAL)), 0) t FROM settings "
                "WHERE key LIKE 'archived_cost.%'").fetchone()
        else:
            row = c.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) t FROM runs WHERE project = ?",
                (project,),
            ).fetchone()
            arch = c.execute(
                "SELECT COALESCE(SUM(CAST(value AS REAL)), 0) t FROM settings "
                "WHERE key = ?", (f"archived_cost.{project}",),
            ).fetchone()
        return row["t"] + arch["t"]


# --- releases ---------------------------------------------------------------

def create_release(project: str, version: str, notes: str,
                   item_keys: list[str]) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO releases (project, version, notes, items_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (project, version, notes, json.dumps(item_keys), now()),
        )
        return cur.lastrowid


def update_release(release_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE releases SET {cols} WHERE id = ?",
                  (*fields.values(), release_id))


def get_release(release_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()


def open_release(project: str):
    with conn() as c:
        return c.execute(
            "SELECT * FROM releases WHERE project = ? AND status = 'proposed' "
            "ORDER BY id DESC LIMIT 1",
            (project,),
        ).fetchone()


def project_releases(project: str):
    with conn() as c:
        return c.execute(
            "SELECT * FROM releases WHERE project = ? ORDER BY id DESC", (project,)
        ).fetchall()


# --- persona memory ----------------------------------------------------------

MEMORY_HARD_CAP = 6000  # chars; Tariq condenses well before this


def append_memory(project: str, key: str, note: str) -> None:
    """Add one remembered line to a desk persona's rolling memory.

    Keys are role-shared: analyst (Ruth), engineering (Malcolm + hires),
    lead, ops (Colin), security (Zaf)."""
    note = " ".join(note.split()).strip()
    if not note:
        return
    scope = f"memory:{key}"
    latest = latest_report(scope, project)
    text = (latest["content"] + "\n" if latest else "") + f"- {note}"
    if len(text) > MEMORY_HARD_CAP:
        text = text[-MEMORY_HARD_CAP:]
        text = text[text.index("\n") + 1:] if "\n" in text else text
    save_report(scope, project, text)


def persona_memory(project: str, key: str) -> str:
    latest = latest_report(f"memory:{key}", project)
    return latest["content"] if latest else ""


# --- reports (CTO / team-lead output) ---------------------------------------

def save_report(scope: str, project: str, content: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO reports (scope, project, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (scope, project, content, now()),
        )


def latest_report(scope: str, project: str = ""):
    with conn() as c:
        return c.execute(
            "SELECT * FROM reports WHERE scope = ? AND project = ? "
            "ORDER BY id DESC LIMIT 1",
            (scope, project),
        ).fetchone()


# --- operator-in-the-loop -------------------------------------------------------

def ask_question(project: str, asked_by: str, item_key: str,
                 question: str, options: list[str] | None = None) -> None:
    question = question.strip()
    if not question:
        return
    opts = json.dumps([o.strip()[:80] for o in (options or []) if o.strip()][:3])
    with conn() as c:
        dup = c.execute(
            "SELECT id FROM questions WHERE project = ? AND status IN "
            "('open', 'escalated') AND question = ?",
            (project, question)).fetchone()
        if dup:
            return
        c.execute(
            "INSERT INTO questions (project, asked_by, item_key, question, "
            "options, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (project, asked_by, item_key, question,
             opts if opts != "[]" else "", now()))
    log_event(f"{asked_by} has a question for the operator: {question[:120]}",
              "warn", project=project)


def answer_question(qid: int, answer: str, by: str = "operator") -> None:
    with conn() as c:
        c.execute("UPDATE questions SET status = 'answered', answer = ?, "
                  "answered_by = ?, answered_at = ? WHERE id = ?",
                  (answer, by, now(), qid))


def escalate_question(qid: int) -> None:
    with conn() as c:
        c.execute("UPDATE questions SET status = 'escalated' WHERE id = ? "
                  "AND status = 'open'", (qid,))


def dismiss_question(qid: int) -> None:
    with conn() as c:
        c.execute("UPDATE questions SET status = 'dismissed', answered_at = ? "
                  "WHERE id = ?", (now(), qid))


def open_questions(project: str | None = None):
    """Questions still pending: escalated (for the operator) first, then those
    sitting with Harry."""
    q = ("SELECT * FROM questions WHERE status IN ('open', 'escalated') "
         "ORDER BY status = 'escalated' DESC, id DESC")
    with conn() as c:
        if project is None:
            return c.execute(q).fetchall()
        return c.execute(q.replace("WHERE", "WHERE project = ? AND"),
                         (project,)).fetchall()


def harry_inbox():
    with conn() as c:
        return c.execute(
            "SELECT * FROM questions WHERE status = 'open' ORDER BY id"
        ).fetchall()


def recent_answers(project: str, limit: int = 8):
    with conn() as c:
        return c.execute(
            "SELECT * FROM questions WHERE status = 'answered' AND project = ? "
            "ORDER BY answered_at DESC LIMIT ?", (project, limit)).fetchall()


def add_direction(project: str, text: str, item_key: str = "") -> None:
    """A standing direction from the operator — stored as a pre-answered question so
    it flows into prompts exactly like answered questions do."""
    text = text.strip()
    if not text:
        return
    with conn() as c:
        c.execute(
            "INSERT INTO questions (project, asked_by, item_key, question, "
            "status, answer, answered_by, created_at, answered_at) VALUES "
            "(?, 'operator', ?, '(standing direction)', 'answered', ?, 'operator', ?, ?)",
            (project, item_key, text, now(), now()))
    log_event(f"Operator direction: {text[:120]}", project=project)


def answers_for(project: str, item_key: str):
    with conn() as c:
        return c.execute(
            "SELECT * FROM questions WHERE status = 'answered' "
            "AND project = ? AND item_key = ? ORDER BY id", 
            (project, item_key)).fetchall()


# --- maintenance mode --------------------------------------------------------

def set_maintenance(reason: str) -> None:
    set_setting("maintenance_reason", reason)
    if reason:
        log_event(f"Maintenance mode on: {reason}", "warn")
    else:
        log_event("Maintenance mode off")


def maintenance() -> str:
    """Non-empty reason while maintenance mode is active. Unlike the API-limit
    pause this has no expiry and no GUI resume — only the operator clears it."""
    return get_setting("maintenance_reason", "")


# --- heartbeat ---------------------------------------------------------------

_last_hb = 0.0
HEARTBEAT_THROTTLE_S = 15


def touch_heartbeat() -> None:
    """Record worker liveness; throttled so streaming callers stay cheap."""
    import time as _time
    global _last_hb
    if _time.time() - _last_hb < HEARTBEAT_THROTTLE_S:
        return
    _last_hb = _time.time()
    set_setting("heartbeat", now())


def heartbeat_age_seconds() -> float | None:
    ts = get_setting("heartbeat")
    if not ts:
        return None
    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


# --- staffing (Harry's hires and stand-downs) --------------------------------

def staff_get(project: str) -> dict:
    raw = get_setting(f"staff.{project}", "")
    if raw:
        try:
            d = json.loads(raw)
            return {"extra": list(d.get("extra", [])),
                    "benched": list(d.get("benched", []))}
        except ValueError:
            pass
    return {"extra": [], "benched": []}


def staff_set(project: str, staff: dict) -> None:
    set_setting(f"staff.{project}", json.dumps(staff))


# --- run control -------------------------------------------------------------

def request_cancel(run_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE runs SET cancel_requested = 1 "
                  "WHERE id = ? AND finished_at IS NULL", (run_id,))


def cancel_requested(run_id: int) -> bool:
    with conn() as c:
        row = c.execute("SELECT cancel_requested FROM runs WHERE id = ?",
                        (run_id,)).fetchone()
        return bool(row and row["cancel_requested"])


def get_run(run_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def consecutive_failures(project: str, item_key: str) -> int:
    """Trailing count of failed runs for this item (cancellations count)."""
    with conn() as c:
        rows = c.execute(
            "SELECT ok FROM runs WHERE project = ? AND item_key = ? "
            "AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 5",
            (project, item_key)).fetchall()
    n = 0
    for r in rows:
        if r["ok"] == 0:
            n += 1
        else:
            break
    return n


def question_options(q) -> list[str]:
    if not q["options"]:
        return []
    try:
        return json.loads(q["options"])
    except ValueError:
        return []
