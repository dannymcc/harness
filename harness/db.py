"""SQLite state store.

One connection per call keeps things simple and safe across the worker task
and web handlers (SQLite serialises writes; volumes are tiny here).

Everything except global settings is scoped to a project (one project = one
managed repository = one harness).
"""
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

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
    --                -> held (with Harry) -> approved | waiting_human | rejected
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
-- latest_report() wants the newest row for one (scope, project); without
-- this it walks the rowid index backwards over every other scope's rows.
CREATE INDEX IF NOT EXISTS reports_scope ON reports(scope, project, id);
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
    answered_at TEXT,
    ruling_passes INTEGER NOT NULL DEFAULT 0  -- ruling passes left undecided
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS thread (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    item_key TEXT NOT NULL,           -- 'issue#123' / 'pr#45' / 'release'
    who TEXT NOT NULL,                -- persona or operator
    kind TEXT NOT NULL DEFAULT 'note',-- note | finding | plan | ruling | direction | test | event
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS thread_item ON thread(project, item_key, id);
CREATE TABLE IF NOT EXISTS steers (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    resolution TEXT NOT NULL DEFAULT ''   -- '' | kept | discarded
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


MIGRATIONS = [
    "ALTER TABLE runs ADD COLUMN agent TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE questions ADD COLUMN answered_by TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE questions ADD COLUMN options TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE items ADD COLUMN repro_test TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE steers ADD COLUMN resolution TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE items ADD COLUMN breaker_reset_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE items ADD COLUMN breaker_trips INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE releases ADD COLUMN error TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE questions ADD COLUMN routed_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE questions ADD COLUMN ruling_passes INTEGER NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS reports_scope ON reports(scope, project, id)",
]

# DB paths this process has already prepared: data directory made, journal
# mode set, schema created and the migration list walked. Keyed on the path,
# not a bare flag, so the tests' per-test DB (conftest monkeypatches
# config.DB_PATH) is still migrated.
_migrated_paths: set[str] = set()


@contextmanager
def conn():
    key = str(config.DB_PATH)
    first = key not in _migrated_paths
    if first:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    # Durability is left at SQLite's default unless the environment says
    # otherwise; see config.DB_SYNCHRONOUS. Genuinely a property of the
    # connection, so it is set on every one, outside the guard below — and
    # set first, so the journal-mode switch below is itself made at the
    # configured durability rather than at SQLite's default FULL. That
    # ordering is most of the win in #85: the switch fsyncs, and the suite
    # gives every test its own database to convert.
    if config.DB_SYNCHRONOUS:
        c.execute(f"PRAGMA synchronous={config.DB_SYNCHRONOUS}")
    if first:
        # Desk cycles run concurrently; WAL lets readers proceed under a
        # writer instead of stacking "database is locked" retries on the 30s
        # timeout. Journal mode is a persistent property of the database
        # file rather than of the connection, so re-issuing it on a path
        # this process has already set touches the header for nothing.
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        for mig in MIGRATIONS:
            try:
                c.execute(mig)
            except sqlite3.OperationalError:
                pass  # already applied
        # Only after a clean pass: a real failure retries on the next conn().
        _migrated_paths.add(key)
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


# --- the stream ---------------------------------------------------------------
# One conversation instead of a dozen lists: events, the operator's directions,
# questions on their way to Harry and the item threads, merged into a single
# feed. Plain dicts rather than sqlite3.Row, because `action_payload` — what an
# inline card needs to act on the row — is a dict.

def stream(project: str | None = None, since: str | None = None,
           kinds=None, limit: int = 200) -> list[dict]:
    """Rows of (ts, project, who, kind, text, item_key, action_payload).

    Newest first — the transcript view reverses them. `project=None` merges
    every desk; a name matches exactly, since section-wide rows (empty project)
    belong to the merged view only. `since` is strictly greater than, `kinds`
    an iterable of kind names (None means all), `limit` caps the result.

    Kinds are `event`, `direction` (the operator's, pending or answered),
    `question` (still with Harry or escalated) and whatever an item thread
    carries: note | finding | plan | ruling | direction | test | event.

    The events `add_direction` and `ask_question` write alongside their
    questions row are dropped: that row is already here under its own kind,
    and a transcript that says everything twice is worse than no transcript.
    """
    wanted = None if kinds is None else set(kinds)
    if wanted is not None and not wanted:
        return []
    selects, args = [], []

    def scoped(where: list[str], vals: list, ts_col: str) -> str:
        """Add the project/since bounds shared by all three sources."""
        if project is not None:
            where.append("project = ?")
            vals.append(project)
        if since:
            where.append(f"{ts_col} > ?")
            vals.append(since)
        args.extend(vals)
        return (" WHERE " + " AND ".join(where)) if where else ""

    if wanted is None or "event" in wanted:
        where = ["message NOT LIKE ?", "message NOT LIKE ?",
                 "message NOT LIKE ?"]
        vals = [DIRECTION_EVENT_PREFIX + "%", "%" + ASK_EVENT_INFIX + "%",
                "%" + ESCALATED_EVENT_INFIX + "%"]
        selects.append(
            "SELECT ts AS ts, project AS project, '' AS who, 'event' AS kind, "
            "message AS text, '' AS item_key, 0 AS src, id AS rid, "
            "NULL AS qid, '' AS status, '' AS answer, '' AS options "
            "FROM events" + scoped(where, vals, "ts"))

    asks = {
        "direction": "(asked_by = 'operator' "
                     "AND status IN ('directive', 'answered'))",
        "question": "(asked_by != 'operator' AND status IN ('open', 'escalated'))",
    }
    picked = [clause for k, clause in asks.items()
              if wanted is None or k in wanted]
    if picked:
        selects.append(
            "SELECT created_at AS ts, project AS project, asked_by AS who, "
            "CASE WHEN asked_by = 'operator' THEN 'direction' ELSE 'question' "
            "END AS kind, question AS text, item_key AS item_key, 1 AS src, "
            "id AS rid, id AS qid, status AS status, answer AS answer, "
            "options AS options FROM questions"
            + scoped(["(" + " OR ".join(picked) + ")"], [], "created_at"))

    where, vals = [], []
    if wanted is not None:
        where.append("kind IN (%s)" % ", ".join("?" * len(wanted)))
        vals.extend(sorted(wanted))
    selects.append(
        "SELECT created_at AS ts, project AS project, who AS who, kind AS kind, "
        "text AS text, item_key AS item_key, 2 AS src, id AS rid, NULL AS qid, "
        "'' AS status, '' AS answer, '' AS options FROM thread"
        + scoped(where, vals, "created_at"))

    # db.now() is second-resolution, so ties are common: break them on each
    # table's own id.
    sql = " UNION ALL ".join(selects) + " ORDER BY ts DESC, src, rid DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        rows = c.execute(sql, args).fetchall()

    out = []
    for r in rows:
        payload = None
        if r["src"] == 1 and r["kind"] == "question":
            payload = {"type": "question", "id": r["qid"], "status": r["status"],
                       "options": question_options(r)}
        elif r["src"] == 1:
            payload = {"type": "direction", "id": r["qid"], "status": r["status"],
                       "reply": r["answer"]}
        out.append({"ts": r["ts"], "project": r["project"], "who": r["who"],
                    "kind": r["kind"], "text": r["text"],
                    "item_key": r["item_key"], "action_payload": payload})
    return out


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


def update_run(run_id: int, **fields) -> None:
    """Progress on a run still in flight — never touches finished_at."""
    cols = ", ".join(f"{k} = ?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE runs SET {cols} WHERE id = ?",
                  (*fields.values(), run_id))


def finish_run(run_id: int, ok: bool, cost_usd: float, turns: int,
               summary: str, log_path: str = "") -> None:
    with conn() as c:
        c.execute(
            """UPDATE runs SET ok = ?, cost_usd = ?, turns = ?, summary = ?,
                              log_path = ?, finished_at = ? WHERE id = ?""",
            (1 if ok else 0, cost_usd, turns, summary, log_path, now(), run_id),
        )
        # Anything the operator queued but the session never took is still
        # theirs to keep or drop — say so rather than losing it quietly.
        stranded = c.execute(
            "SELECT COUNT(*) AS n FROM steers WHERE run_id = ? "
            "AND delivered_at IS NULL AND resolution = ''", (run_id,)
        ).fetchone()["n"]
        project = c.execute("SELECT project FROM runs WHERE id = ?",
                            (run_id,)).fetchone()
        project = project["project"] if project else ""
    if stranded:  # outside the conn(), which holds the write lock
        log_event(f"Run {run_id} ended with {stranded} undelivered "
                  f"steer{'' if stranded == 1 else 's'} — keep or discard "
                  "them on the run page", "warn", project=project)


def mark_no_effect(run_id: int, summary: str) -> None:
    """Record a finished run as failed after the fact, once the caller has
    seen that it achieved nothing.

    A session that returns structured output has completed cleanly, so
    `finish_run` records ok=1 — but "the agent came back" and "the item
    moved" are different things. An engineer that declines the work, or
    reports success while leaving the worktree untouched, has produced
    nothing the item can use, and `consecutive_failures` (which reads only
    runs.ok) would otherwise count that as a healthy run and forget every
    failure before it.

    Deliberately narrow: only ok and summary change. started_at and
    finished_at are left exactly as `finish_run` wrote them, so the
    orphaned-run skipping and the `breaker_reset_at` window in
    `consecutive_failures` behave as before.
    """
    with conn() as c:
        c.execute(
            "UPDATE runs SET ok = 0, summary = ? "
            "WHERE id = ? AND finished_at IS NOT NULL",
            (summary[:300], run_id))


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
            "SELECT * FROM releases WHERE project = ? AND status IN "
            "('proposed', 'merging') ORDER BY id DESC LIMIT 1",
            (project,),
        ).fetchone()


def last_release(project: str):
    """The most recently released release, or None if there has never been one.

    Only rows that actually went out count: a proposed or abandoned release
    is not a cut, so it never anchors a scheduled release window.
    """
    with conn() as c:
        return c.execute(
            "SELECT * FROM releases WHERE project = ? AND status = 'released' "
            "AND released_at IS NOT NULL AND released_at != '' "
            "ORDER BY released_at DESC LIMIT 1",
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
# Both writers below log an event alongside their questions row, so the plain
# activity list still shows them. `stream()` drops those derived events again —
# it has the questions row itself. The shapes live here as constants so the
# writer and the filter cannot drift apart.

DIRECTION_EVENT_PREFIX = "Operator direction: "
ASK_EVENT_INFIX = " has asked Harry: "
ESCALATED_EVENT_INFIX = " has escalated to the operator: "

# An answered question stays answered. Re-filing the same question about the
# same item inside this window is refused: the earlier answer is already on
# the item's thread, which every agent prompt carries, so the asker gets the
# decision instead of the operator getting the question again. A week covers
# an item's working life and still lets a genuinely new circumstance ask
# afresh later.
ANSWER_DEDUP_DAYS = 7

# What an answer tells the harness to do with the item it is about. Small,
# literal, exact-match on the normalised text: an answer moves an item only
# when it says so in as many words, so the option buttons agents offer
# ("Fix", "Skip", "Won't fix") carry their meaning and nothing has to be
# inferred later. A bare "yes" is deliberately absent — it answers the
# question, not the item's fate. Anything unmatched is a message to the
# agent that asked, and pipeline.route_answers sends the item back to them.
ANSWER_ACTIONS = {
    # get on with it — the operator saying "fix" is the same act as pressing
    # approve, whatever the fix_issues policy says
    "fix": "proceed", "fix it": "proceed", "fix this": "proceed",
    "yes fix": "proceed", "yes fix it": "proceed", "go ahead": "proceed",
    "proceed": "proceed", "do it": "proceed", "approve": "proceed",
    "approved": "proceed", "get on with it": "proceed", "merge": "proceed",
    "merge it": "proceed", "ship it": "proceed",
    # leave it where it is, still the operator's
    "skip": "hold", "skip it": "hold", "skip for now": "hold",
    "not now": "hold", "later": "hold", "leave it": "hold", "hold": "hold",
    "hold it": "hold", "wait": "hold", "park it": "hold", "no": "hold",
    # done with — off the board
    "dont fix": "reject", "do not fix": "reject", "wont fix": "reject",
    "wontfix": "reject", "reject": "reject", "reject it": "reject",
    "close": "reject", "close it": "reject", "not a bug": "reject",
    "decline": "reject",
}

_PUNCT = re.compile(r"[^a-z0-9 ]+")


def _normalise(text: str) -> str:
    """Lowercased, apostrophe- and punctuation-free, single-spaced.

    Used both for matching an answer against ANSWER_ACTIONS and for deciding
    whether a question is the same question as one already answered."""
    text = (text or "").lower().replace("'", "").replace("’", "")
    return " ".join(_PUNCT.sub(" ", text).split())


_LEAD_SPLIT = re.compile(r"\s+[—–-]+\s+|[:;,.!?\n(]")


def answer_action(answer: str) -> str:
    """'proceed' | 'hold' | 'reject', or '' when the wording doesn't say.

    The whole answer is tried first, then its leading clause — the words
    before the first dash, colon, full stop or line break. A ruling reads
    "Won't fix — close it out, and to be clear that is delivered" far more
    often than a bare "Won't fix", and matching only the bare form sent
    such rulings back to the asker as "wording says nothing either way",
    which re-dispatched an item its ruling had just closed."""
    whole = _normalise(answer)
    if whole in ANSWER_ACTIONS:
        return ANSWER_ACTIONS[whole]
    lead = _normalise(_LEAD_SPLIT.split(answer or "", 1)[0])
    return ANSWER_ACTIONS.get(lead, "") if lead != whole else ""


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ask_question(project: str, asked_by: str, item_key: str,
                 question: str, options: list[str] | None = None) -> int | None:
    """File a question. Returns its id, or None if empty/duplicate.

    Harry cannot rule on his own question, so anything he asks is filed as
    escalated: it is the operator's by definition. Filing it 'open' would
    leave it in nobody's hands — `harry_inbox()` excludes his own rows, so
    it would never be ruled on and never reach the operator either.

    A question already answered for this project and item within
    ANSWER_DEDUP_DAYS counts as a duplicate too: asking again over a live
    answer is how an operator ends up being asked the same thing forever."""
    question = question.strip()
    if not question:
        return None
    own = asked_by == config.CTO_NAME
    opts = json.dumps([o.strip()[:80] for o in (options or []) if o.strip()][:3])
    with conn() as c:
        dup = c.execute(
            "SELECT id FROM questions WHERE project = ? AND status IN "
            "('open', 'escalated') AND question = ?",
            (project, question)).fetchone()
        if dup:
            return None
        answered = c.execute(
            "SELECT question FROM questions WHERE project = ? AND item_key = ? "
            "AND status = 'answered' AND COALESCE(answered_at, '') >= ?",
            (project, item_key, _days_ago(ANSWER_DEDUP_DAYS))).fetchall()
        settled = any(_normalise(r["question"]) == _normalise(question)
                      for r in answered)
        qid = None
        if not settled:
            cur = c.execute(
                "INSERT INTO questions (project, asked_by, item_key, question, "
                "options, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project, asked_by, item_key, question,
                 opts if opts != "[]" else "",
                 "escalated" if own else "open", now()))
            qid = cur.lastrowid
    if settled:
        about = f" about {item_key}" if item_key else ""
        log_event(f"{asked_by} asked something already answered{about} — not "
                  f"put to anyone again; the answer stands: {question[:100]}",
                  project=project)
        return None
    infix = ESCALATED_EVENT_INFIX if own else ASK_EVENT_INFIX
    log_event(f"{asked_by}{infix}{question[:120]}", project=project)
    return qid


def answer_question(qid: int, answer: str, by: str = "operator") -> None:
    with conn() as c:
        c.execute("UPDATE questions SET status = 'answered', answer = ?, "
                  "answered_by = ?, answered_at = ? WHERE id = ?",
                  (answer, by, now(), qid))
        q = c.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
    if q and q["item_key"] and q["project"]:
        thread_append(q["project"], q["item_key"], by, "ruling",
                      f"Q ({q['asked_by']}): {q['question']}\nA: {answer}")


def unrouted_answers(project: str):
    """Answered questions about an item whose answer has not been acted on.

    `routed_at` is stamped once an answer has moved (or been weighed and
    deliberately not moved) its item, so an answer acts exactly once. Rows
    written before that column existed read as unrouted, which is how items
    stranded by the old behaviour — answered, but never picked up — get
    re-entered on the first cycle after this lands."""
    with conn() as c:
        return c.execute(
            "SELECT * FROM questions WHERE project = ? AND status = 'answered' "
            "AND item_key != '' AND COALESCE(routed_at, '') = '' ORDER BY id",
            (project,)).fetchall()


def mark_question_routed(qid: int) -> None:
    with conn() as c:
        c.execute("UPDATE questions SET routed_at = ? WHERE id = ?",
                  (now(), qid))


def answers_since(project: str, since: str = ""):
    """The operator's answers landed since `since` ('' means all of them).

    Harry's rulings are excluded: he answers his people constantly and his
    answer reaches them through the question record. An answer from the
    operator is news to the desk."""
    with conn() as c:
        return c.execute(
            "SELECT * FROM questions WHERE project = ? AND status = 'answered' "
            "AND answered_by != ? AND COALESCE(answered_at, '') > ? "
            "ORDER BY id", (project, config.CTO_NAME, since)).fetchall()


def bump_ruling_passes(qid: int) -> int:
    """Count one ruling pass that left this question open, and return the new
    total.

    The count lives on the row rather than in process memory: the harness
    restarts on every release, and a counter that resets with the process
    would let a question nobody rules on cycle for ever instead of reaching
    the operator. Rows written before the column existed read as 0, so the
    worst an upgrade costs a pending question is one more pass."""
    with conn() as c:
        c.execute("UPDATE questions SET ruling_passes = ruling_passes + 1 "
                  "WHERE id = ?", (qid,))
        row = c.execute("SELECT ruling_passes FROM questions WHERE id = ?",
                        (qid,)).fetchone()
    return row["ruling_passes"] if row else 0


def escalate_question(qid: int) -> None:
    with conn() as c:
        c.execute("UPDATE questions SET status = 'escalated' WHERE id = ? "
                  "AND status = 'open'", (qid,))


def question(qid: int):
    with conn() as c:
        return c.execute("SELECT * FROM questions WHERE id = ?",
                         (qid,)).fetchone()


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


def harry_inbox(project: str | None = None):
    """Questions sitting with Harry (not yet ruled on or escalated)."""
    q = ("SELECT * FROM questions WHERE status = 'open' "
         "AND asked_by != 'Harry' ORDER BY id")
    with conn() as c:
        if project is None:
            return c.execute(q).fetchall()
        return c.execute(q.replace("WHERE", "WHERE project = ? AND"),
                         (project,)).fetchall()


def escalated_questions(project: str | None = None):
    """Questions Harry has escalated — the only ones that need the operator."""
    q = "SELECT * FROM questions WHERE status = 'escalated' ORDER BY id DESC"
    with conn() as c:
        if project is None:
            return c.execute(q).fetchall()
        return c.execute(q.replace("WHERE", "WHERE project = ? AND"),
                         (project,)).fetchall()


def harry_prior_question(project: str, item_key: str, since: str):
    """Harry's own question about this item (or, for an item-less one, this
    desk) that is still with the operator, or that they answered after
    `since` — whatever its wording. None when he has a clean slate.

    ask_question's own dedupe is on the text, and Harry rephrases every
    hour; the stand-up asks about a thing, so the thing is the key."""
    with conn() as c:
        return c.execute(
            "SELECT * FROM questions WHERE project = ? AND item_key = ? "
            "AND asked_by = ? AND (status IN ('open', 'escalated') OR "
            "(status = 'answered' AND COALESCE(answered_at, '') >= ?)) "
            "ORDER BY status = 'answered', id DESC LIMIT 1",
            (project, item_key, config.CTO_NAME, since)).fetchone()


def operator_rulings_since(since: str):
    """The operator's answers to Harry's own questions since `since`, across
    every desk — what the stand-up digest carries back so he acts on a
    ruling instead of asking for it again."""
    with conn() as c:
        return c.execute(
            "SELECT * FROM questions WHERE asked_by = ? AND status = 'answered' "
            "AND answered_by != ? AND COALESCE(answered_at, '') >= ? "
            "ORDER BY answered_at DESC LIMIT 12",
            (config.CTO_NAME, config.CTO_NAME, since)).fetchall()


def recent_answers(project: str, limit: int = 8):
    with conn() as c:
        return c.execute(
            "SELECT * FROM questions WHERE status = 'answered' AND project = ? "
            "ORDER BY answered_at DESC LIMIT ?", (project, limit)).fetchall()


def add_direction(project: str, text: str, item_key: str = "",
                  note_thread: bool = True) -> None:
    """An operator direction: pending until Harry turns it into actions.

    The direction text is the question; Harry's acknowledgement becomes the
    answer, after which it flows into prompt digests like any ruling.
    `note_thread=False` skips the thread line for callers whose text is
    already in the thread — keeping an undelivered steer, which the steer
    box mirrored there when it was sent."""
    text = text.strip()
    if not text:
        return
    with conn() as c:
        c.execute(
            "INSERT INTO questions (project, asked_by, item_key, question, "
            "status, answer, answered_by, created_at) VALUES "
            "(?, 'operator', ?, ?, 'directive', '', '', ?)",
            (project, item_key, text, now()))
    if item_key and note_thread:
        thread_append(project, item_key, config.OPERATOR, "direction", text)
    log_event(f"{DIRECTION_EVENT_PREFIX}{text[:120]}", project=project)


def pending_directives(project: str | None = None):
    q = "SELECT * FROM questions WHERE status = 'directive' ORDER BY id"
    with conn() as c:
        if project is None:
            return c.execute(q).fetchall()
        return c.execute(q.replace("WHERE", "WHERE project = ? AND"),
                         (project,)).fetchall()


def resolve_directive(qid: int, reply: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE questions SET status = 'answered', answered_by = 'Harry', "
            "answer = ?, answered_at = ? WHERE id = ? AND status = 'directive'",
            (reply.strip()[:1500], now(), qid))


def recent_directions(project: str, limit: int = 3):
    with conn() as c:
        return c.execute(
            "SELECT * FROM questions WHERE project = ? "
            "AND asked_by = 'operator' "
            "AND status IN ('directive', 'answered') "
            "ORDER BY id DESC LIMIT ?", (project, limit)).fetchall()


def item_directions(project: str, item_key: str, since: str = ""):
    """Operator directions filed against one item, oldest first.

    Reading the thread and filtering on kind would also pick up the mid-run
    steer mirrors, which are a different thing, so ask the table directly.
    `since` bounds it to directions filed from that timestamp on — the run
    page uses the run's own start, so it shows what was queued during it."""
    q = ("SELECT * FROM questions WHERE project = ? AND item_key = ? "
         "AND asked_by = 'operator'")
    args = [project, item_key]
    if since:
        q += " AND created_at >= ?"
        args.append(since)
    with conn() as c:
        return c.execute(q + " ORDER BY id", args).fetchall()


def answers_for(project: str, item_key: str):
    with conn() as c:
        return c.execute(
            "SELECT * FROM questions WHERE status = 'answered' "
            "AND project = ? AND item_key = ? ORDER BY id", 
            (project, item_key)).fetchall()


# --- item threads -------------------------------------------------------------
# One running conversation per item: Ruth's findings, the plan, Harry's
# rulings, the operator's directions, the engineer's notes, test results.
# Every agent touching the item reads it whole and appends to it — the
# hand-off artefact, rather than fields scattered across columns.

THREAD_MAX_CHARS = 14000


def thread_append(project: str, item_key: str, who: str, kind: str,
                  text: str) -> None:
    text = (text or "").strip()
    if not text or not item_key:
        return
    with conn() as c:
        c.execute(
            "INSERT INTO thread (project, item_key, who, kind, text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (project, item_key, who, kind, text[:20000], now()))


def thread(project: str, item_key: str):
    with conn() as c:
        return c.execute(
            "SELECT * FROM thread WHERE project = ? AND item_key = ? ORDER BY id",
            (project, item_key)).fetchall()


def thread_text(project: str, item_key: str,
                max_chars: int = THREAD_MAX_CHARS) -> str:
    """The thread rendered for a prompt, newest entries kept whole if it has
    to be cut (old context is summarised by position, not dropped silently)."""
    rows = thread(project, item_key)
    if not rows:
        return ""
    parts = [f"[{r['created_at'][5:16].replace('T', ' ')}] {r['who']} ({r['kind']}):\n{r['text']}"
             for r in rows]
    out = "\n\n".join(parts)
    if len(out) <= max_chars:
        return out
    kept, size = [], 0
    for part in reversed(parts):
        if size + len(part) > max_chars:
            break
        kept.append(part)
        size += len(part) + 2
    dropped = len(parts) - len(kept)
    return (f"[{dropped} earlier entr{'y' if dropped == 1 else 'ies'} omitted "
            "for length]\n\n" + "\n\n".join(reversed(kept)))


# --- steering a running agent ---------------------------------------------------

def add_steer(run_id: int, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    with conn() as c:
        c.execute("INSERT INTO steers (run_id, text, created_at) VALUES (?, ?, ?)",
                  (run_id, text, now()))


def take_steers(run_id: int):
    """Undelivered steers for a run, marked delivered on the way out.

    A steer the operator has already kept or discarded on the run page is
    settled: it must never reappear in a session."""
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM steers WHERE run_id = ? AND delivered_at IS NULL "
            "AND resolution = '' ORDER BY id", (run_id,)).fetchall()
        if rows:
            c.execute("UPDATE steers SET delivered_at = ? WHERE run_id = ? "
                      "AND delivered_at IS NULL AND resolution = ''",
                      (now(), run_id))
    return rows


def run_steers(run_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM steers WHERE run_id = ? ORDER BY id",
                         (run_id,)).fetchall()


def undelivered_steers(run_id: int):
    """Steers the session never took and the operator has not settled."""
    with conn() as c:
        return c.execute(
            "SELECT * FROM steers WHERE run_id = ? AND delivered_at IS NULL "
            "AND resolution = '' ORDER BY id", (run_id,)).fetchall()


def get_steer(steer_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM steers WHERE id = ?",
                         (steer_id,)).fetchone()


def resolve_steer(steer_id: int, resolution: str) -> None:
    """Settle an undelivered steer: kept as a direction, or discarded.

    Stamping delivered_at too keeps a settled steer out of take_steers even
    if a later session somehow asks for the same run."""
    with conn() as c:
        c.execute(
            "UPDATE steers SET resolution = ?, delivered_at = ? "
            "WHERE id = ? AND resolution = ''", (resolution, now(), steer_id))


def spend_since(project: str, since_iso: str) -> float:
    with conn() as c:
        r = c.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM runs "
                      "WHERE project = ? AND started_at >= ?",
                      (project, since_iso)).fetchone()
        return float(r[0] or 0)


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
                    "benched": list(d.get("benched", [])),
                    "hired_at": dict(d.get("hired_at", {}))}
        except ValueError:
            pass
    return {"extra": [], "benched": [], "hired_at": {}}


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


def run_persona(run, lead_name: str = "") -> str:
    """The name the GUI shows for a run's agent.

    `runs.agent` is filled in by the desk cycle, but rows written before that
    column existed — and any written by a path that doesn't name the agent —
    leave it empty, so fall back to the persona the role and task imply, as
    the staff board does."""
    return run["agent"] or config.persona(run["role"], run["task"], lead_name)


def live_runs(project: str, agent: str = ""):
    """Runs still in flight on a desk, newest first.

    `agent` is matched case-insensitively against the display name, so
    "malcolm" finds Malcolm's run. Used by the composer's /tell and /stop to
    turn a name the operator knows into a run id."""
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM runs WHERE finished_at IS NULL AND project = ? "
            "ORDER BY id DESC", (project,)).fetchall()
    if not agent:
        return rows
    p = get_project(project)
    lead = p["lead_name"] if p else ""
    want = agent.strip().lower()
    return [r for r in rows if run_persona(r, lead).lower() == want]


ORPHANED_SUMMARY = "orphaned by restart"


def consecutive_failures(project: str, item_key: str) -> int:
    """Trailing count of failed runs for this item (cancellations count).

    Runs orphaned by a restart are skipped, not counted: the process was
    killed under the agent, which says nothing about the item. Counting
    them meant two deploys in a row tripped the circuit breaker on
    whatever happened to be in flight.

    Failures from before the item's last deliberate approval are also
    ignored: an operator (or Harry) re-approving a held item is saying
    "try again" — counting the old failures re-held the item and paged
    the operator before any new attempt had run.
    """
    kind, _, number = item_key.partition("#")
    reset = ""
    with conn() as c:
        it = c.execute("SELECT breaker_reset_at FROM items WHERE project = ? "
                       "AND kind = ? AND number = ?",
                       (project, kind, number)).fetchone()
        if it:
            reset = it["breaker_reset_at"] or ""
        rows = c.execute(
            "SELECT ok, summary, started_at FROM runs WHERE project = ? "
            "AND item_key = ? AND finished_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 8",
            (project, item_key)).fetchall()
    n = 0
    for r in rows:
        if reset and r["started_at"] <= reset:
            break
        if r["summary"] == ORPHANED_SUMMARY:
            continue
        if r["ok"] == 0:
            n += 1
        else:
            break
    return n


def recent_failures(project: str, item_key: str, limit: int = 2):
    """The item's most recent failed runs, newest first.

    The run summary of a failed run is the cause as the SDK reported it
    ("error_max_turns: …", "stopped by the operator"), which is what a
    ruling on a held item turns on — whether the work is too big for one
    session or genuinely broken."""
    with conn() as c:
        return c.execute(
            "SELECT * FROM runs WHERE project = ? AND item_key = ? "
            "AND finished_at IS NOT NULL AND ok = 0 AND summary != ? "
            "ORDER BY id DESC LIMIT ?",
            (project, item_key, ORPHANED_SUMMARY, limit)).fetchall()


def question_options(q) -> list[str]:
    if not q["options"]:
        return []
    try:
        return json.loads(q["options"])
    except ValueError:
        return []
