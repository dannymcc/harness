import datetime as dt
from unittest.mock import patch


def test_circuit_breaker(fresh_db, may):
    from harness import pipeline
    fresh_db.upsert_item("may", "issue", 9, "flaky", "a", "open", "x")
    for _ in range(2):
        rid = fresh_db.start_run("may", "ic", "issue#9", "fix", "m", "Malcolm")
        fresh_db.finish_run(rid, False, 0.1, 1, "boom")
    item = fresh_db.get_item("may", "issue", 9)
    assert pipeline._breaker_tripped(may, item) is True
    assert fresh_db.get_item("may", "issue", 9)["status"] == "waiting_human"
    rid = fresh_db.start_run("may", "ic", "issue#9", "fix", "m", "Malcolm")
    fresh_db.finish_run(rid, True, 0.1, 1, "ok")
    assert fresh_db.consecutive_failures("may", "issue#9") == 0


def test_staffing_guard_same_day(fresh_db, may):
    from harness import pipeline
    pipeline._apply_staffing([
        {"project": "may", "action": "hire", "name": "Beth", "reason": "x"}])
    pipeline._apply_staffing([
        {"project": "may", "action": "stand_down", "name": "Beth", "reason": "idle"}])
    st = fresh_db.staff_get("may")
    assert "Beth" in st["extra"] and "Beth" not in st["benched"]


def test_staffing_caps_and_pool(fresh_db, may):
    from harness import pipeline
    pipeline._apply_staffing([
        {"project": "may", "action": "hire", "name": n, "reason": "x"}
        for n in ("Dimitri", "Beth", "Erin", "NotInPool")])
    assert fresh_db.staff_get("may")["extra"] == ["Dimitri", "Beth"]


def test_active_hours(fresh_db, may):
    from harness import pipeline

    class At3(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 1, 1, 3, 0, tzinfo=tz)

    fresh_db.set_policy("may", "active_hours", "08-23")
    with patch.object(pipeline, "datetime", At3):
        assert pipeline.within_active_hours("may") is False
    fresh_db.set_policy("may", "active_hours", "22-06")
    with patch.object(pipeline, "datetime", At3):
        assert pipeline.within_active_hours("may") is True
    for val in ("always", "", "garbage"):
        fresh_db.set_policy("may", "active_hours", val)
        assert pipeline.within_active_hours("may") is True


def test_directives_and_staffing_requests(fresh_db, may):
    from harness import pipeline
    fresh_db.set_setting("directives.may", "- Clear the backlog")
    assert "Clear the backlog" in pipeline._state_digest(may)
    fresh_db.set_setting("staffing_request.may", "one more engineer")
    assert "STAFFING REQUEST from Tom" in pipeline._standup_digest()


def test_release_due_thresholds(fresh_db, may):
    from harness import pipeline
    assert pipeline._release_due(may) == []
    for n in (1, 2, 3):
        fresh_db.upsert_item("may", "issue", n, "t", "a", "open", "x")
        fresh_db.update_item("may", "issue", n, status="queued",
                             queued_at=fresh_db.now())
    assert len(pipeline._release_due(may)) == 3


def test_restart_recovery(fresh_db, may):
    from harness import worker
    rid = fresh_db.start_run("may", "ic", "issue#5", "fix", "m", "Malcolm")
    fresh_db.upsert_item("may", "issue", 5, "t", "a", "open", "x")
    fresh_db.update_item("may", "issue", 5, status="working")
    worker.recover_after_restart()
    assert fresh_db.get_run(rid)["ok"] == 0
    assert fresh_db.get_item("may", "issue", 5)["status"] == "approved"


def test_fix_failures_retry_then_breaker(fresh_db, may):
    """Mechanical failures requeue; the breaker holds after two in a row."""
    from harness import pipeline
    fresh_db.upsert_item("may", "issue", 20, "flaky", "a", "open", "x")
    fresh_db.update_item("may", "issue", 20, status="approved")
    rid = fresh_db.start_run("may", "ic", "issue#20", "fix", "m", "Malcolm")
    fresh_db.finish_run(rid, False, 0.1, 1, "transport crash")
    # one failure: breaker not yet tripped, item may retry
    item = fresh_db.get_item("may", "issue", 20)
    assert pipeline._breaker_tripped(may, item) is False
    rid2 = fresh_db.start_run("may", "ic", "issue#20", "fix", "m", "Malcolm")
    fresh_db.finish_run(rid2, False, 0.1, 1, "transport crash again")
    assert pipeline._breaker_tripped(may, item) is True
    assert fresh_db.get_item("may", "issue", 20)["status"] == "waiting_human"


def test_directive_actions_executor(fresh_db, may):
    from harness import pipeline
    fresh_db.upsert_item("may", "issue", 30, "t", "a", "open", "x")
    fresh_db.update_item("may", "issue", 30, status="waiting_human")
    fresh_db.upsert_item("may", "issue", 31, "t2", "a", "open", "x")
    fresh_db.update_item("may", "issue", 31, status="queued",
                         queued_at=fresh_db.now())
    fresh_db.ask_question("may", "Ruth", "", "Which way?")
    qid = fresh_db.open_questions("may")[0]["id"]
    done = pipeline._apply_directive_actions(may, [
        {"action": "approve_item", "kind": "issue", "number": 30},
        {"action": "hire", "name": "Erin"},
        {"action": "security_review"},
        {"action": "propose_release"},
        {"action": "set_policy", "key": "merge_prs", "value": "auto"},
        {"action": "set_policy", "key": "bogus", "value": "x"},
        {"action": "tell_desk", "text": "Prioritise the API work"},
        {"action": "answer_question", "question_id": qid, "text": "This way"},
        {"action": "approve_item", "kind": "issue", "number": 999},
    ])
    assert fresh_db.get_item("may", "issue", 30)["status"] == "approved"
    assert "Erin" in fresh_db.staff_get("may")["extra"]
    assert fresh_db.get_setting("security_requested.may") == "1"
    assert pipeline._release_due(may)  # operator request forces it
    assert fresh_db.policy("may", "merge_prs") == "auto"
    assert fresh_db.policy("may", "fix_issues") == "auto"  # bogus key ignored
    assert "Prioritise the API work" in fresh_db.get_setting("directives.may")
    assert fresh_db.answers_for("may", "")[0]["answered_by"] == "Harry"
    assert len(done) == 7  # two invalid actions skipped


def test_directive_lifecycle(fresh_db, may):
    fresh_db.add_direction("may", "Hold everything until Monday")
    pend = fresh_db.pending_directives("may")
    assert len(pend) == 1 and pend[0]["question"] == "Hold everything until Monday"
    fresh_db.resolve_directive(pend[0]["id"], "Held the queue; nothing lands before Monday.")
    assert not fresh_db.pending_directives("may")
    d = fresh_db.recent_directions("may")[0]
    assert d["answer"].startswith("Held the queue")
