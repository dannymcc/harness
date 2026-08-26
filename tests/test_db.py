import sqlite3


class _CountingConnection(sqlite3.Connection):
    """Connection that records schema and migration statements it is asked
    to run. sqlite3.Connection is an immutable C type, so its methods can't
    be monkeypatched — a factory subclass is the way to count them."""

    schema_runs = 0
    migration_runs = 0
    wal_runs = 0
    synchronous_runs = 0

    def executescript(self, sql):
        type(self).schema_runs += 1
        return super().executescript(sql)

    def execute(self, sql, *args, **kwargs):
        from harness import db
        if sql in db.MIGRATIONS:  # not every migration is an ALTER TABLE
            type(self).migration_runs += 1
        if sql == "PRAGMA journal_mode=WAL":
            type(self).wal_runs += 1
        if sql.startswith("PRAGMA synchronous="):
            type(self).synchronous_runs += 1
        return super().execute(sql, *args, **kwargs)


def test_conn_runs_schema_and_migrations_once_per_db_path(fresh_db,
                                                          monkeypatch):
    """db.conn() must not replay the full schema + migration list on every
    call — only the first conn() against a given DB path should pay that
    cost (issue #73)."""
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: real_connect(
        *a, factory=_CountingConnection, **kw))
    _CountingConnection.schema_runs = 0
    _CountingConnection.migration_runs = 0

    for _ in range(5):
        with fresh_db.conn() as c:
            c.execute("SELECT 1")

    assert _CountingConnection.schema_runs == 1, (
        f"schema executescript ran {_CountingConnection.schema_runs} times "
        "over 5 conn() calls against the same DB path; expected 1"
    )
    assert _CountingConnection.migration_runs == len(fresh_db.MIGRATIONS), (
        f"migrations ran {_CountingConnection.migration_runs} times over 5 "
        f"conn() calls; expected one pass of {len(fresh_db.MIGRATIONS)}"
    )


def test_conn_skips_wal_pragma_reissue_after_first_call(fresh_db,
                                                        monkeypatch):
    """PRAGMA journal_mode=WAL is a persistent property of the database file
    once set — re-issuing it on every conn() call touches the DB header and
    can force a checkpoint, for no benefit after the first call against a
    given path (issue #85). Only the first conn() against a given DB path
    should pay that cost, mirroring the once-per-path schema/migration guard
    from #73. PRAGMA synchronous is genuinely per-connection, so it must
    still be issued on every connection."""
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: real_connect(
        *a, factory=_CountingConnection, **kw))
    _CountingConnection.wal_runs = 0
    _CountingConnection.synchronous_runs = 0

    for _ in range(5):
        with fresh_db.conn() as c:
            c.execute("SELECT 1")

    assert _CountingConnection.wal_runs == 1, (
        f"PRAGMA journal_mode=WAL ran {_CountingConnection.wal_runs} times "
        "over 5 conn() calls against the same DB path; expected 1"
    )
    # ...and the journal mode really is WAL on a later connection, which is
    # the whole reason the re-issue is safe to skip.
    with fresh_db.conn() as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert _CountingConnection.synchronous_runs == 6, (
        "config.DB_SYNCHRONOUS must be honoured on every connection, not "
        f"once per path; it ran {_CountingConnection.synchronous_runs} times "
        "over 6 conn() calls"
    )


def test_conn_migrates_each_distinct_db_path(fresh_db, monkeypatch, tmp_path):
    """A cache keyed only on 'has anything ever been migrated this process'
    (e.g. a bare module-level boolean) would leave a second, distinct DB
    path unmigrated. The cache must be keyed on the resolved config.DB_PATH
    so tests/conftest.py's per-test fresh_db fixture stays isolated."""
    from harness import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "a.db")
    with fresh_db.conn() as c:
        c.execute("SELECT 1 FROM items")  # schema must exist for path a

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "b.db")
    with fresh_db.conn() as c:
        c.execute("SELECT 1 FROM items")  # schema must also exist for path b


def test_policies_defaults_and_override(fresh_db, may):
    assert fresh_db.policy("may", "merge_prs") == "approve"
    fresh_db.set_policy("may", "merge_prs", "auto")
    assert fresh_db.policy("may", "merge_prs") == "auto"


def test_lead_assignment_round_robin(fresh_db, may):
    fresh_db.create_project("second", "example/second")
    assert fresh_db.get_project("may")["lead_name"] == "Tom"
    assert fresh_db.get_project("second")["lead_name"] == "Adam"


def test_item_lifecycle(fresh_db, may):
    fresh_db.upsert_item("may", "issue", 1, "t", "a", "open", "x")
    fresh_db.update_item("may", "issue", 1, status="queued", queued_at=fresh_db.now())
    assert [i["number"] for i in fresh_db.items_by_status("may", "queued")] == [1]


def test_total_cost_includes_archived(fresh_db, may):
    rid = fresh_db.start_run("may", "ic", "issue#1", "fix", "m", "Malcolm")
    fresh_db.finish_run(rid, True, 1.5, 3, "ok")
    fresh_db.set_setting("archived_cost.may", "2.5")
    assert abs(fresh_db.total_cost("may") - 4.0) < 1e-9


def test_questions_flow(fresh_db, may):
    fresh_db.ask_question("may", "Ruth", "issue#1", "Gate it?", options=["Yes", "No"])
    q = fresh_db.open_questions("may")[0]
    assert fresh_db.question_options(q) == ["Yes", "No"]
    fresh_db.ask_question("may", "Ruth", "issue#1", "Gate it?")  # dedup
    assert len(fresh_db.open_questions("may")) == 1
    fresh_db.escalate_question(q["id"])
    assert fresh_db.open_questions("may")[0]["status"] == "escalated"
    fresh_db.answer_question(q["id"], "Yes", by="Harry")
    assert fresh_db.answers_for("may", "issue#1")[0]["answered_by"] == "Harry"


def test_harrys_own_question_is_filed_escalated(fresh_db, may):
    """Whoever the caller is — the ask_harry tool inside his own session
    included — a question from Harry goes to the operator at filing. Filed
    'open' it would be in nobody's hands: harry_inbox() skips his rows."""
    qid = fresh_db.ask_question("may", "Harry", "issue#1", "Drop the runner?")
    assert fresh_db.question(qid)["status"] == "escalated"
    assert fresh_db.harry_inbox("may") == []
    assert [q["id"] for q in fresh_db.escalated_questions("may")] == [qid]
    assert any(m["message"].startswith("Harry has escalated to the operator: ")
               for m in fresh_db.recent_events())
    # and the derived event is dropped from the stream, which has the row
    texts = [r["text"] for r in fresh_db.stream(project="may")]
    assert sum("Drop the runner?" in t for t in texts) == 1


def test_persona_memory_append_and_cap(fresh_db, may):
    fresh_db.append_memory("may", "analyst", "remember this")
    assert "remember this" in fresh_db.persona_memory("may", "analyst")
    for i in range(500):
        fresh_db.append_memory("may", "analyst", f"note {i} padding padding")
    assert len(fresh_db.persona_memory("may", "analyst")) <= fresh_db.MEMORY_HARD_CAP


def test_prune_bounds_reports_per_scope_and_project(fresh_db, may):
    """The reports table only ever grows (issue #78): housekeeping.prune()
    must keep it bounded per (scope, project) rather than letting every
    memory note / lead summary / stand-up / security report accumulate
    forever, or latest_report's backwards scan degrades as the section
    runs."""
    from harness import housekeeping

    for i in range(20):
        fresh_db.save_report("security", "may", f"report {i}")

    housekeeping.prune()

    with fresh_db.conn() as c:
        rows = c.execute(
            "SELECT content FROM reports WHERE scope = ? AND project = ? "
            "ORDER BY id",
            ("security", "may"),
        ).fetchall()

    assert len(rows) < 20, (
        "prune() should bound reports per (scope, project); "
        f"found {len(rows)} rows still present after inserting 20"
    )
    assert rows[-1]["content"] == "report 19"
    assert rows[0]["content"] != "report 0", "oldest rows should be pruned first"
    assert fresh_db.latest_report("security", "may")["content"] == "report 19"


def test_persona_memory_survives_prune(fresh_db, may):
    """Pruning must not eat live memory: after prune() runs, persona_memory
    still returns the accumulated notes and still respects MEMORY_HARD_CAP."""
    from harness import housekeeping

    fresh_db.append_memory("may", "analyst", "remember this")
    for i in range(500):
        fresh_db.append_memory("may", "analyst", f"note {i} padding padding")

    housekeeping.prune()

    mem = fresh_db.persona_memory("may", "analyst")
    assert mem, "memory should survive pruning"
    assert len(mem) <= fresh_db.MEMORY_HARD_CAP


def test_db_synchronous_is_off_for_tests_and_default_otherwise(fresh_db,
                                                               monkeypatch):
    """conftest turns fsync-per-commit off for the suite's throwaway
    databases — ~100ms a write, and the bulk of the suite's runtime. With
    the environment unset, db.conn() must leave durability exactly as it
    was: SQLite's own FULL. The value is interpolated into a PRAGMA, so it
    is only ever one of a fixed set."""
    from harness import config

    assert config.DB_SYNCHRONOUS == "OFF"  # set by tests/conftest.py
    assert config.DB_SYNCHRONOUS in ("", "OFF", "NORMAL", "FULL", "EXTRA")

    monkeypatch.setattr(config, "DB_SYNCHRONOUS", "")
    with fresh_db.conn() as c:
        assert c.execute("PRAGMA synchronous").fetchone()[0] == 2, "not FULL"


def test_reports_lookup_is_indexed(fresh_db):
    """latest_report() must seek, not walk the rowid index backwards over
    every other scope's rows — on a new DB and, via MIGRATIONS, on one
    created before the index existed."""
    with fresh_db.conn() as c:
        plan = " ".join(r["detail"] for r in c.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM reports WHERE scope = ? "
            "AND project = ? ORDER BY id DESC LIMIT 1", ("security", "may")))
    assert "reports_scope" in plan, f"latest_report is unindexed: {plan}"
    assert any("reports_scope" in m for m in fresh_db.MIGRATIONS), (
        "the index must also be a migration, or existing databases never "
        "get it"
    )


def test_stream_unions_events_questions_and_thread(fresh_db, may):
    """One chronological feed over events, directions/questions and threads.

    Rows are plain dicts (action_payload is a dict, so sqlite3.Row won't do),
    newest first, with the derived events that db.add_direction and
    db.ask_question write alongside their rows dropped — otherwise the
    transcript says everything twice.
    """
    fresh_db.log_event("Cycle finished", project="may")
    fresh_db.add_direction("may", "Focus on bugs this week")
    fresh_db.ask_question("may", "Ruth", "issue#1", "Ship it?", options=["A", "B"])
    q = fresh_db.open_questions("may")[0]
    fresh_db.escalate_question(q["id"])
    fresh_db.thread_append("may", "issue#1", "Ruth", "finding", "It is a bug.")
    fresh_db.log_event("Another desk entirely", project="june")

    # db.now() is second-resolution, so stamp the rows to make order and the
    # `since` window deterministic rather than a race against the clock.
    with fresh_db.conn() as c:
        c.execute("UPDATE events SET ts = '2026-01-01T10:00:00Z' "
                  "WHERE message = 'Cycle finished'")
        c.execute("UPDATE questions SET created_at = '2026-01-01T10:01:00Z' "
                  "WHERE asked_by = 'operator'")
        c.execute("UPDATE questions SET created_at = '2026-01-01T10:02:00Z' "
                  "WHERE asked_by = 'Ruth'")
        c.execute("UPDATE thread SET created_at = '2026-01-01T10:03:00Z'")

    rows = fresh_db.stream(project="may")
    texts = [r["text"] for r in rows]
    assert sum("Focus on bugs this week" in t for t in texts) == 1  # not twice
    assert sum("Ship it?" in t for t in texts) == 1
    assert not any("Another desk entirely" in t for t in texts)  # other desk
    assert {"event", "direction", "question", "finding"} <= {r["kind"] for r in rows}
    assert set(rows[0]) >= {"ts", "project", "who", "kind", "text", "item_key",
                            "action_payload"}
    assert [r["ts"] for r in rows] == sorted((r["ts"] for r in rows), reverse=True)
    assert rows[0]["text"] == "It is a bug."
    assert rows[0]["who"] == "Ruth" and rows[0]["item_key"] == "issue#1"

    # an escalated question carries what an inline card needs to act on it
    esc = next(r for r in rows if r["kind"] == "question")
    assert esc["action_payload"]["id"] == q["id"]
    assert esc["action_payload"]["options"] == ["A", "B"]

    pending = fresh_db.stream(project="may", kinds=("direction",))
    assert len(pending) == 1 and pending[0]["text"] == "Focus on bugs this week"
    recent = fresh_db.stream(project="may", since="2026-01-01T10:01:30Z")
    assert len(recent) == 2 and all(r["ts"] > "2026-01-01T10:01:30Z" for r in recent)
    assert len(fresh_db.stream(project="may", limit=1)) == 1
    assert any(r["project"] == "june" for r in fresh_db.stream())  # merged view


def test_stream_scoping_and_direction_payload(fresh_db, may):
    """Section-wide rows are the merged view's, and an answered direction
    carries Harry's reply for the collapsed card."""
    fresh_db.log_event("Section-wide notice")            # no project
    fresh_db.add_direction("may", "Ship the release")
    qid = fresh_db.pending_directives("may")[0]["id"]
    fresh_db.resolve_directive(qid, "On it.")
    fresh_db.thread_append("may", "issue#1", "Harry", "ruling", "Approved.")

    on_desk = fresh_db.stream(project="may")
    assert not any(r["text"] == "Section-wide notice" for r in on_desk)
    assert any(r["text"] == "Section-wide notice" for r in fresh_db.stream())

    direction = next(r for r in on_desk if r["kind"] == "direction")
    assert direction["action_payload"] == {"type": "direction", "id": qid,
                                           "status": "answered", "reply": "On it."}
    ruling = next(r for r in on_desk if r["kind"] == "ruling")
    assert ruling["action_payload"] is None and ruling["who"] == "Harry"
    assert [r["kind"] for r in fresh_db.stream(project="may", kinds=("ruling",))] \
        == ["ruling"]
