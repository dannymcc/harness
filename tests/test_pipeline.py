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
