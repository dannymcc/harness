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
