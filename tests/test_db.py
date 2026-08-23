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


def test_persona_memory_append_and_cap(fresh_db, may):
    fresh_db.append_memory("may", "analyst", "remember this")
    assert "remember this" in fresh_db.persona_memory("may", "analyst")
    for i in range(500):
        fresh_db.append_memory("may", "analyst", f"note {i} padding padding")
    assert len(fresh_db.persona_memory("may", "analyst")) <= fresh_db.MEMORY_HARD_CAP
