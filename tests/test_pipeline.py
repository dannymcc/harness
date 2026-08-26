import datetime as dt
from unittest.mock import patch


def _fail_twice(db, key="issue#9", first="boom", second="boom again"):
    for summary in (first, second):
        rid = db.start_run("may", "ic", key, "fix", "m", "Malcolm")
        db.finish_run(rid, False, 0.1, 1, summary)


def test_circuit_breaker_goes_to_harry_not_the_operator(fresh_db, may,
                                                        monkeypatch):
    """The first trip is a question for Harry, carrying both failures. The
    operator hears nothing: that is his ruling to make, not theirs."""
    from harness import pipeline
    paged = []
    monkeypatch.setattr(pipeline.notify, "send",
                        lambda *a, **k: paged.append(a))
    fresh_db.upsert_item("may", "issue", 9, "flaky", "a", "open", "x")
    _fail_twice(fresh_db, first="error_max_turns: ran out",
                second="error_max_turns: again")
    item = fresh_db.get_item("may", "issue", 9)
    assert pipeline._breaker_tripped(may, item) is True
    held = fresh_db.get_item("may", "issue", 9)
    assert held["status"] == "held" and held["breaker_trips"] == 1
    assert paged == []
    q = fresh_db.harry_inbox("may")[0]
    assert q["asked_by"] == "harness" and q["item_key"] == "issue#9"
    failures = q["question"].split("The failures:", 1)[1].split("Rule on it")[0]
    assert failures.count("error_max_turns") == 2   # both runs' error kinds
    assert fresh_db.question_options(q) == ["retry", "split", "escalate"]
    rid = fresh_db.start_run("may", "ic", "issue#9", "fix", "m", "Malcolm")
    fresh_db.finish_run(rid, True, 0.1, 1, "ok")
    assert fresh_db.consecutive_failures("may", "issue#9") == 0


def test_harry_rules_retry_on_a_held_item(fresh_db, may, monkeypatch):
    """A retry ruling re-approves the item with a cleared session — and does
    not forgive the trip, so it buys exactly one more attempt."""
    import asyncio
    from harness import agents, pipeline
    monkeypatch.setattr(pipeline.notify, "send", lambda *a, **k: None)
    fresh_db.upsert_item("may", "issue", 9, "flaky", "a", "open", "x")
    fresh_db.update_item("may", "issue", 9, status="working",
                         session_id="sess-1")
    _fail_twice(fresh_db)
    pipeline._breaker_tripped(may, fresh_db.get_item("may", "issue", 9))
    qid = fresh_db.harry_inbox("may")[0]["id"]

    async def fake_rule(inbox, ctx):
        return {"ok": True, "error": "", "output": {"summary": "", "decisions": [
            {"question_id": qid, "action": "answer", "item_action": "retry",
             "answer": "Worth one clean run."}]}}
    monkeypatch.setattr(agents, "rule_questions", fake_rule)
    asyncio.run(pipeline.process_questions("may"))

    after = fresh_db.get_item("may", "issue", 9)
    assert after["status"] == "approved" and after["session_id"] == ""
    assert after["breaker_trips"] == 1          # the trip stands
    assert fresh_db.consecutive_failures("may", "issue#9") == 0


def test_second_trip_after_a_ruling_goes_to_the_operator(fresh_db, may,
                                                         monkeypatch):
    """The hard floor: Harry gets one ruling per item, then it is the
    operator's, whatever he says."""
    from harness import pipeline
    paged = []
    monkeypatch.setattr(pipeline.notify, "send",
                        lambda *a, **k: paged.append(a))
    fresh_db.upsert_item("may", "issue", 9, "flaky", "a", "open", "x")
    fresh_db.update_item("may", "issue", 9, breaker_trips=1)
    _fail_twice(fresh_db)
    item = fresh_db.get_item("may", "issue", 9)
    assert pipeline._breaker_tripped(may, item) is True
    after = fresh_db.get_item("may", "issue", 9)
    assert after["status"] == "waiting_human" and after["breaker_trips"] == 2
    assert len(paged) == 1                      # the operator is paged
    assert fresh_db.harry_inbox("may") == []    # and Harry is not asked again


def test_harry_rules_split_on_a_held_item(fresh_db, may, monkeypatch):
    """Split reaches the team lead as a directive; the item stays held
    rather than being retried into the same wall."""
    import asyncio
    from harness import agents, pipeline
    monkeypatch.setattr(pipeline.notify, "send", lambda *a, **k: None)
    fresh_db.upsert_item("may", "issue", 9, "huge", "a", "open", "x")
    _fail_twice(fresh_db)
    pipeline._breaker_tripped(may, fresh_db.get_item("may", "issue", 9))
    qid = fresh_db.harry_inbox("may")[0]["id"]

    async def fake_rule(inbox, ctx):
        return {"ok": True, "error": "", "output": {"summary": "", "decisions": [
            {"question_id": qid, "action": "answer", "item_action": "split",
             "answer": "Too big for one run — break it into three."}]}}
    monkeypatch.setattr(agents, "rule_questions", fake_rule)
    asyncio.run(pipeline.process_questions("may"))

    assert "issue#9" in fresh_db.get_setting("directives.may")
    assert "break it into three" in fresh_db.get_setting("directives.may")
    assert fresh_db.get_item("may", "issue", 9)["status"] == "held"


def test_harry_escalating_a_held_item_pages_the_operator(fresh_db, may,
                                                         monkeypatch):
    import asyncio
    from harness import agents, pipeline
    paged = []
    monkeypatch.setattr(pipeline.notify, "send",
                        lambda *a, **k: paged.append(a))
    fresh_db.upsert_item("may", "issue", 9, "odd", "a", "open", "x")
    _fail_twice(fresh_db)
    pipeline._breaker_tripped(may, fresh_db.get_item("may", "issue", 9))
    assert paged == []
    qid = fresh_db.harry_inbox("may")[0]["id"]

    async def fake_rule(inbox, ctx):
        return {"ok": True, "error": "", "output": {"summary": "", "decisions": [
            {"question_id": qid, "action": "escalate", "answer": ""}]}}
    monkeypatch.setattr(agents, "rule_questions", fake_rule)
    asyncio.run(pipeline.process_questions("may"))

    assert len(fresh_db.escalated_questions("may")) == 1
    assert len(paged) == 1
    assert fresh_db.get_item("may", "issue", 9)["status"] == "waiting_human"


def test_a_ruling_without_a_direction_lands_on_the_operators_desk(
        fresh_db, may, monkeypatch):
    """An answer that moves nothing would leave the item held with nobody
    acting; it goes to the operator instead."""
    import asyncio
    from harness import agents, pipeline
    paged = []
    monkeypatch.setattr(pipeline.notify, "send",
                        lambda *a, **k: paged.append(a))
    fresh_db.upsert_item("may", "issue", 9, "odd", "a", "open", "x")
    _fail_twice(fresh_db)
    pipeline._breaker_tripped(may, fresh_db.get_item("may", "issue", 9))
    qid = fresh_db.harry_inbox("may")[0]["id"]

    async def fake_rule(inbox, ctx):
        return {"ok": True, "error": "", "output": {"summary": "", "decisions": [
            {"question_id": qid, "action": "answer", "answer": "Noted."}]}}
    monkeypatch.setattr(agents, "rule_questions", fake_rule)
    asyncio.run(pipeline.process_questions("may"))

    assert fresh_db.get_item("may", "issue", 9)["status"] == "waiting_human"
    assert len(paged) == 1


def test_restart_orphans_do_not_trip_the_breaker(fresh_db, may):
    """Two deploys in a row killed whatever was in flight; that is not the
    item's fault and must not hold it."""
    from harness import pipeline, worker
    fresh_db.upsert_item("may", "issue", 9, "fine", "a", "open", "x")
    for _ in range(2):
        fresh_db.start_run("may", "ic", "issue#9", "fix", "m", "Malcolm")
        worker.recover_after_restart()
    assert fresh_db.consecutive_failures("may", "issue#9") == 0
    item = fresh_db.get_item("may", "issue", 9)
    assert pipeline._breaker_tripped(may, item) is False
    # a real failure either side of an orphan still counts as consecutive
    rid = fresh_db.start_run("may", "ic", "issue#9", "fix", "m", "Malcolm")
    fresh_db.finish_run(rid, False, 0.1, 1, "boom")
    fresh_db.start_run("may", "ic", "issue#9", "fix", "m", "Malcolm")
    worker.recover_after_restart()
    rid = fresh_db.start_run("may", "ic", "issue#9", "fix", "m", "Malcolm")
    fresh_db.finish_run(rid, False, 0.1, 1, "boom again")
    assert fresh_db.consecutive_failures("may", "issue#9") == 2


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
    assert pipeline._release_due(may) is None
    for n in (1, 2, 3):
        fresh_db.upsert_item("may", "issue", n, "t", "a", "open", "x")
        fresh_db.update_item("may", "issue", n, status="queued",
                             queued_at=fresh_db.now())
    assert len(pipeline._release_due(may)) == 3


def test_anything_to_release_is_what_the_button_asks(fresh_db, may,
                                                     monkeypatch):
    """The GUI offers Release now on this answer, so it has to match what a
    request would actually do."""
    from harness import pipeline, repo
    monkeypatch.setattr(repo, "dev_ahead_count", lambda project: 0)
    assert pipeline.anything_to_release(may) is False
    fresh_db.upsert_item("may", "issue", 1, "t", "a", "open", "x")
    fresh_db.update_item("may", "issue", 1, status="queued",
                         queued_at=fresh_db.now())
    assert pipeline.anything_to_release(may) is True
    # nothing queued, but dev carries work that landed outside the harness
    fresh_db.update_item("may", "issue", 1, status="released")
    monkeypatch.setattr(repo, "dev_ahead_count", lambda project: 3)
    assert pipeline.anything_to_release(may) is True


def test_operator_release_with_nothing_queued(fresh_db, may, monkeypatch):
    """Work landed on dev outside the harness: pressing Release now must
    still cut one, and must not claim there is a release when there isn't."""
    from harness import pipeline, repo
    monkeypatch.setattr(repo, "dev_ahead_count", lambda project: 2)
    fresh_db.set_setting("release_requested.may", "1")
    assert pipeline._release_due(may) == []          # empty, but a real yes
    assert fresh_db.get_setting("release_requested.may") == ""  # consumed

    monkeypatch.setattr(repo, "dev_ahead_count", lambda project: 0)
    fresh_db.set_setting("release_requested.may", "1")
    assert pipeline._release_due(may) is None
    assert "nothing to release" in fresh_db.recent_events(5, "may")[0]["message"]


def _days_ago(n):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc)
            - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _released(fresh_db, project, version, days_ago):
    """A release that actually went out, that many days ago."""
    rid = fresh_db.create_release(project, version, "notes", [])
    fresh_db.update_release(rid, status="released",
                            released_at=_days_ago(days_ago))
    return rid


def _queue(fresh_db, project, numbers):
    for n in numbers:
        fresh_db.upsert_item(project, "issue", n, "t", "a", "open", "x")
        fresh_db.update_item(project, "issue", n, status="queued",
                             queued_at=fresh_db.now())


def test_release_schedule_defaults_to_the_thresholds(fresh_db, may):
    """The upgrade must not move a single live project onto a clock."""
    from harness import config, pipeline
    assert config.POLICY_DEFAULTS["release_schedule"] == "changes"
    assert fresh_db.policy("may", "release_schedule") == "changes"
    assert pipeline.release_window_days("may") is None
    _queue(fresh_db, "may", (1, 2, 3))
    assert len(pipeline._release_due(may)) == 3   # count trigger, as before


def test_weekly_release_schedule_ignores_count_and_anchors_to_last_release(
        fresh_db, may):
    """On a weekly cadence the count and age thresholds do not apply: the
    only question is how long it is since the last release went out."""
    from harness import pipeline
    fresh_db.set_policy("may", "release_schedule", "weekly")
    rid = _released(fresh_db, "may", "1.0.0", days_ago=2)
    _queue(fresh_db, "may", (1, 2, 3, 4, 5))   # well over release_min_changes
    assert pipeline._release_due(may) is None   # day 2: not yet

    fresh_db.update_release(rid, released_at=_days_ago(8))
    assert len(pipeline._release_due(may)) == 5  # day 8: the whole week's work


def test_weekly_release_schedule_skips_empty_windows_silently(fresh_db, may,
                                                              monkeypatch):
    """A window with nothing in it is not a release and not a warning."""
    from harness import pipeline, repo
    monkeypatch.setattr(repo, "dev_ahead_count", lambda project: 0)
    fresh_db.set_policy("may", "release_schedule", "weekly")
    _released(fresh_db, "may", "1.0.0", days_ago=30)
    assert pipeline._release_due(may) is None
    assert not [e for e in fresh_db.recent_events(20, "may")
                if e["level"] == "warn"]


def test_first_release_on_a_schedule_waits_for_something_to_release(
        fresh_db, may, monkeypatch):
    """No release yet: due on the first cycle that has anything to cut."""
    from harness import pipeline, repo
    monkeypatch.setattr(repo, "dev_ahead_count", lambda project: 0)
    fresh_db.set_policy("may", "release_schedule", "monthly")
    assert pipeline._release_due(may) is None
    _queue(fresh_db, "may", (1,))
    assert len(pipeline._release_due(may)) == 1


def test_missed_release_window_gives_one_catch_up_release(fresh_db, may,
                                                          monkeypatch):
    """Four weeks off the air is one catch-up release, not four."""
    from harness import pipeline, repo
    monkeypatch.setattr(repo, "dev_ahead_count", lambda project: 0)
    fresh_db.set_policy("may", "release_schedule", "weekly")
    _released(fresh_db, "may", "1.0.0", days_ago=30)
    _queue(fresh_db, "may", (1, 2))
    assert len(pipeline._release_due(may)) == 2
    # ...and that release goes out, taking the queue with it
    _released(fresh_db, "may", "1.1.0", days_ago=0)
    for n in (1, 2):
        fresh_db.update_item("may", "issue", n, status="released")
    assert pipeline._release_due(may) is None    # no back-dated burst


def test_operator_release_now_overrides_the_schedule(fresh_db, may):
    """Release now and Harry's propose_release cut whatever the cadence."""
    from harness import pipeline
    fresh_db.set_policy("may", "release_schedule", "monthly")
    _released(fresh_db, "may", "1.0.0", days_ago=1)
    _queue(fresh_db, "may", (1,))
    assert pipeline._release_due(may) is None
    fresh_db.set_setting("release_requested.may", "1")
    assert len(pipeline._release_due(may)) == 1


def test_digest_describes_the_live_release_trigger(fresh_db, may):
    from harness import pipeline
    digest = pipeline._state_digest(may)
    assert "3 changes are queued or the oldest is 7 days old" in digest
    fresh_db.set_policy("may", "release_schedule", "weekly")
    digest = pipeline._state_digest(may)
    assert "a week has passed since the last release" in digest
    assert "3 changes" not in digest


def test_no_release_proposed_while_one_is_open(fresh_db, may):
    from harness import pipeline
    fresh_db.upsert_item("may", "issue", 1, "t", "a", "open", "x")
    fresh_db.update_item("may", "issue", 1, status="queued",
                         queued_at=fresh_db.now())
    fresh_db.create_release("may", "1.2.0", "notes", ["issue#1"])
    fresh_db.set_setting("release_requested.may", "1")
    assert pipeline._release_due(may) is None
    # the request survives for after this one lands, rather than being eaten
    assert fresh_db.get_setting("release_requested.may") == "1"


def test_auto_cut_release_marks_merging_before_finalising(fresh_db, may,
                                                          monkeypatch):
    """Auto releases must not sit in 'proposed' while they finalise, or the
    GUI offers an approve button for a release already being merged."""
    import asyncio
    from harness import pipeline, repo

    drafted, seen = {}, {}

    class _NoLock:
        def __enter__(self): return None
        def __exit__(self, *a): return False

    async def _fake_propose(project, queued):
        return drafted["rid"]

    def _fake_finalize(project, release):
        seen["status"] = fresh_db.get_release(drafted["rid"])["status"]

    monkeypatch.setattr(repo, "clone_lock", lambda project: _NoLock())
    monkeypatch.setattr(pipeline, "_propose_release_locked", _fake_propose)
    monkeypatch.setattr(pipeline, "finalize_release", _fake_finalize)

    drafted["rid"] = fresh_db.create_release("may", "2.0.0", "notes", [])
    fresh_db.set_policy("may", "cut_release", "auto")
    asyncio.run(pipeline.propose_release(may, []))
    assert seen["status"] == "merging"

    # on approve, the operator's click is still what moves it
    seen.clear()
    drafted["rid"] = fresh_db.create_release("may", "2.0.1", "notes", [])
    fresh_db.set_policy("may", "cut_release", "approve")
    asyncio.run(pipeline.propose_release(may, []))
    assert seen == {}
    assert fresh_db.get_release(drafted["rid"])["status"] == "proposed"


def test_refused_merge_puts_the_reason_on_the_release(fresh_db, may,
                                                      monkeypatch):
    """A refused merge (branch protection, red CI, token scope) used to leave
    the release stuck at 'merging' with the cause only in the event log. It
    must come back to 'proposed' carrying the reason, and a later attempt
    that succeeds must clear it."""
    from harness import gh, notify, pipeline, repo

    class _NoLock:
        def __enter__(self): return None
        def __exit__(self, *a): return False

    monkeypatch.setattr(repo, "clone_lock", lambda project: _NoLock())
    monkeypatch.setattr(notify, "send", lambda *a, **k: None)

    def _refuse(*a, **k):
        raise gh.CmdError(["gh", "pr", "merge", "12"], 1, "",
                          "required check 'test' is failing")

    monkeypatch.setattr(gh, "merge_pr", _refuse)
    rid = fresh_db.create_release("may", "3.0.0", "notes", [])
    fresh_db.update_release(rid, pr_number=12, status="merging")
    pipeline.finalize_release(may, fresh_db.get_release(rid))

    rel = fresh_db.get_release(rid)
    assert rel["status"] == "proposed"          # clickable again, not stuck
    assert "required check 'test' is failing" in rel["error"]

    monkeypatch.setattr(gh, "merge_pr", lambda *a, **k: None)
    monkeypatch.setattr(gh, "publish_release", lambda *a, **k: None)
    monkeypatch.setattr(gh, "run", lambda *a, **k: "")
    monkeypatch.setattr(repo, "clean_checkout", lambda project, branch: "/tmp")
    pipeline.finalize_release(may, fresh_db.get_release(rid))

    rel = fresh_db.get_release(rid)
    assert rel["status"] == "released" and rel["error"] == ""


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
    assert fresh_db.get_item("may", "issue", 20)["status"] == "held"


def test_dead_session_resumes_fresh_in_the_same_run(fresh_db, may, monkeypatch, tmp_path):
    """A resume against a session that didn't survive a container restart
    ('No conversation found ...') must fall back to a fresh attempt in the
    same cycle, not burn a cycle and a circuit-breaker count waiting for the
    next one. (Issue #27.)"""
    import asyncio
    from harness import agents, gh, pipeline, repo

    fresh_db.upsert_item("may", "issue", 30, "flaky io", "a", "open", "x")
    fresh_db.update_item("may", "issue", 30, status="approved", plan="do it",
                         session_id="stale-session-id")

    monkeypatch.setattr(gh, "issue_detail",
                        lambda repo_, number: {"number": 30, "title": "t",
                                               "body": "b"})
    monkeypatch.setattr(repo, "add_worktree",
                        lambda project, branch: (tmp_path, ""))
    monkeypatch.setattr(repo, "wt_has_changes", lambda project, wt: True)
    monkeypatch.setattr(repo, "run_tests",
                        lambda project, cwd=None, setup=True, scratch=None:
                        (True, "ok"))
    monkeypatch.setattr(repo, "wt_diff",
                        lambda project, wt: ("1 file changed", "diff"))
    monkeypatch.setattr(repo, "wt_commit_all",
                        lambda project, wt, message: None)
    monkeypatch.setattr(repo, "remove_worktree", lambda project, wt: None)
    monkeypatch.setattr(repo, "push_worktree_to_dev",
                        lambda project, wt, branch: (True, ""))

    resumes_seen = []

    async def fake_fix_issue(project, issue, plan, cwd, resume=None,
                             persona="Malcolm", repro_path=""):
        resumes_seen.append(resume)
        rid = fresh_db.start_run("may", "ic", "issue#30", "fix", "m", persona)
        if resume:
            # The session transcript lived in the container filesystem and
            # didn't survive the restart that interrupted the earlier run.
            err = f"No conversation found with session ID: {resume}"
            fresh_db.finish_run(rid, False, 0.1, 1, err)
            return {"ok": False, "output": None, "session_id": "", "error": err}
        fresh_db.finish_run(rid, True, 0.1, 1, "fixed it")
        return {"ok": True, "error": "", "session_id": "new-session-id",
                "output": {"success": True, "summary": "fixed it",
                          "docs_updated": False, "notes": "",
                          "commit_message": "fix: issue #30 (#30)"}}

    monkeypatch.setattr(agents, "fix_issue", fake_fix_issue)

    item = fresh_db.get_item("may", "issue", 30)
    asyncio.run(pipeline.fix_item(may, item))

    # Retried fresh within this same call, instead of leaving it for the
    # worker's next cycle.
    assert resumes_seen == ["stale-session-id", None]
    after = fresh_db.get_item("may", "issue", 30)
    assert after["session_id"] == "new-session-id"
    assert after["status"] == "queued"
    # The dead session's resume failure did not cost the item a
    # circuit-breaker count.
    assert fresh_db.consecutive_failures("may", "issue#30") == 0
    assert pipeline._breaker_tripped(may, after) is False


def test_dead_session_retries_once_and_only_for_that_error(fresh_db, may,
                                                           monkeypatch,
                                                           tmp_path):
    """The fresh-start fallback fires once, and only on a lost session: an
    ordinary crash still requeues for the next cycle on the first failure."""
    import asyncio
    from harness import agents, gh, pipeline, repo

    monkeypatch.setattr(gh, "issue_detail",
                        lambda repo_, number: {"number": number, "title": "t",
                                               "body": "b"})
    monkeypatch.setattr(repo, "add_worktree",
                        lambda project, branch: (tmp_path, ""))

    calls = []

    def failing(error):
        async def fake_fix_issue(project, issue, plan, cwd, resume=None,
                                 persona="Malcolm", repro_path=""):
            calls.append(resume)
            return {"ok": False, "output": None, "session_id": "", "error": error}
        return fake_fix_issue

    # A session that stays lost: one fallback attempt, then the usual requeue.
    fresh_db.upsert_item("may", "issue", 31, "t", "a", "open", "x")
    fresh_db.update_item("may", "issue", 31, status="approved", plan="do it",
                         session_id="stale")
    monkeypatch.setattr(agents, "fix_issue",
                        failing("No conversation found with session ID: stale"))
    asyncio.run(pipeline.fix_item(may, fresh_db.get_item("may", "issue", 31)))
    assert calls == ["stale", None]
    assert fresh_db.get_item("may", "issue", 31)["status"] == "approved"

    # An ordinary mechanical failure is not a lost session — no second call.
    calls.clear()
    fresh_db.upsert_item("may", "issue", 32, "t", "a", "open", "x")
    fresh_db.update_item("may", "issue", 32, status="approved", plan="do it",
                         session_id="live")
    monkeypatch.setattr(agents, "fix_issue", failing("transport crash"))
    asyncio.run(pipeline.fix_item(may, fresh_db.get_item("may", "issue", 32)))
    assert calls == ["live"]


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


def test_directive_create_issue(fresh_db, may, monkeypatch):
    from harness import pipeline, gh
    monkeypatch.setattr(gh, "create_issue", lambda repo, t, b: 41)
    done = pipeline._apply_directive_actions(may, [
        {"action": "create_issue", "title": "Add CSV export",
         "text": "Export the cost report as CSV."},
        {"action": "create_issue", "title": "no body"},  # skipped
    ])
    assert done == ["opened issue#41: Add CSV export"]
    assert fresh_db.get_item("may", "issue", 41)["status"] == "new"


def test_work_ready_guards(fresh_db, may):
    """work_ready drives the fast re-wake, so it must be narrow: fresh
    approvals and new items yes; errored retries, items the lead has already
    seen, approve-policy waits, budget holds and off-hours no."""
    from harness import pipeline
    fresh_db.set_policy("may", "fix_issues", "lead")
    fresh_db.upsert_item("may", "issue", 3, "bug", "a", "open", "x")
    fresh_db.update_item("may", "issue", 3, status="triaged")
    # nothing planned yet → first look → ready
    assert pipeline.work_ready(may) is True
    # the lead has since been asked to plan (whether or not the plan worked)
    fresh_db.set_setting("last_plan_at.may", "2999-01-01T00:00:00Z")
    assert pipeline.work_ready(may) is False
    # a retry (approved with an error) must not spin the worker
    fresh_db.update_item("may", "issue", 3, status="approved", error="tests failed")
    assert pipeline.work_ready(may) is False
    fresh_db.update_item("may", "issue", 3, status="approved", error="")
    assert pipeline.work_ready(may) is True
    # a new item is ready (triage is a reflex) — unless its triage errored
    fresh_db.update_item("may", "issue", 3, status="new", error="boom")
    assert pipeline.work_ready(may) is False
    fresh_db.update_item("may", "issue", 3, status="new", error="")
    assert pipeline.work_ready(may) is True
    # approve policy: triaged waits for the operator, not the worker
    fresh_db.set_policy("may", "fix_issues", "approve")
    fresh_db.update_item("may", "issue", 3, status="triaged")
    assert pipeline.work_ready(may) is False
    # budget hold and off-hours win over everything
    fresh_db.update_item("may", "issue", 3, status="approved", error="")
    fresh_db.set_setting("budget_hold.may", "1")
    assert pipeline.work_ready(may) is False
    fresh_db.set_setting("budget_hold.may", "")
    import datetime as dt
    from unittest.mock import patch

    class At3(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 1, 1, 3, 0, tzinfo=tz)
    fresh_db.set_policy("may", "active_hours", "08-23")
    with patch.object(pipeline, "datetime", At3):
        assert pipeline.work_ready(may) is False


def test_desk_events_and_budget(fresh_db, may):
    from harness import pipeline
    fresh_db.set_setting("last_plan_at.may", "2999-01-01T00:00:00Z")
    assert pipeline.desk_events(may) == []
    fresh_db.set_setting("directives.may", "- do the thing")
    assert "directive" in pipeline.desk_events(may)[0]
    fresh_db.set_setting("directives.may", "")
    fresh_db.set_policy("may", "fix_issues", "lead")
    fresh_db.upsert_item("may", "issue", 3, "bug", "a", "open", "x")
    fresh_db.update_item("may", "issue", 3, status="triaged")
    fresh_db.set_setting("last_plan_at.may", "2000-01-01T00:00:00Z")
    reasons = pipeline.desk_events(may)
    assert any("sign-off" in r for r in reasons)
    assert any("routine review" in r for r in reasons)
    # budget governor: spend in the last 24h at/over the cap holds the desk
    fresh_db.set_policy("may", "daily_budget_usd", "1")
    rid = fresh_db.start_run("may", "ic", "issue#3", "fix", "m", "Malcolm")
    fresh_db.finish_run(rid, True, 1.5, 3, "ok")
    assert pipeline._budget_hold(may) is True
    assert fresh_db.get_setting("budget_hold.may") == "1"
    assert pipeline.work_ready(may) is False
    fresh_db.set_policy("may", "daily_budget_usd", "100")
    assert pipeline._budget_hold(may) is False
    assert fresh_db.get_setting("budget_hold.may") == ""


def test_harrys_own_question_goes_to_operator(fresh_db, may):
    from harness import pipeline
    pipeline._file_question("", "Harry", "", {"question_for_human": "Budget?"})
    assert fresh_db.harry_inbox() == []
    assert [q["question"] for q in fresh_db.escalated_questions()] == ["Budget?"]


def test_standup_question_is_the_operators_not_harrys_own_inbox(
        fresh_db, may, monkeypatch):
    """Stand-up is Harry speaking: what he cannot decide there is an
    escalation, not a row he would have to rule on himself."""
    import asyncio
    from harness import pipeline, agents

    async def fake_standup(digest):
        return {"ok": True, "error": "", "output": {
            "standup_markdown": "# Stand-up",
            "all_clear": True,
            "desks": [], "blockers": [], "decisions": [],
            "staffing": [], "directives": [],
            "question_for_human": "Do we keep paying for the flaky runner?",
            "question_options": ["Keep", "Drop"]}}

    monkeypatch.setattr(agents, "standup", fake_standup)
    asyncio.run(pipeline.run_standup(force=True))
    assert fresh_db.harry_inbox() == []          # never his own to rule on
    esc = fresh_db.escalated_questions()
    assert [q["question"] for q in esc] == ["Do we keep paying for the "
                                            "flaky runner?"]
    assert esc[0]["asked_by"] == "Harry"
    assert fresh_db.question_options(esc[0]) == ["Keep", "Drop"]
    # and the activity log reads as an escalation, not a self-dialogue
    msgs = [e["message"] for e in fresh_db.recent_events()]
    assert not any(" has asked Harry: " in m for m in msgs)
    assert any(m.startswith("Harry has escalated to the operator: ")
               for m in msgs)


def test_undecided_questions_escalate_after_two_passes(fresh_db, may, monkeypatch):
    import asyncio
    from harness import pipeline, agents
    fresh_db.ask_question("may", "Ruth", "", "Dodged")
    async def dodge(inbox, ctx):
        return {"ok": True, "error": "", "output": {"summary": "", "decisions": []}}
    monkeypatch.setattr(agents, "rule_questions", dodge)
    monkeypatch.setattr(pipeline.notify, "send", lambda *a, **k: None)
    asyncio.run(pipeline.process_questions("may"))
    assert len(fresh_db.harry_inbox("may")) == 1
    asyncio.run(pipeline.process_questions("may"))
    assert fresh_db.harry_inbox("may") == []
    assert len(fresh_db.escalated_questions("may")) == 1


def test_harry_rules_promptly_and_escalates(fresh_db, may, monkeypatch):
    """Questions go to Harry first; his rulings bind the asker, and only
    escalations reach the operator."""
    import asyncio
    from harness import pipeline, agents
    fresh_db.ask_question("may", "Ruth", "issue#1", "Link or plain text?",
                          options=["Link", "Plain"])
    fresh_db.ask_question("may", "Adam", "", "Drop the old API?")
    ids = {q["question"]: q["id"] for q in fresh_db.harry_inbox("may")}
    seen = {}

    async def fake_rule(inbox, ctx):
        seen["inbox"] = inbox
        return {"ok": True, "error": "", "output": {
            "summary": "ruled",
            "decisions": [
                {"question_id": ids["Link or plain text?"], "action": "answer",
                 "answer": "Plain text."},
                {"question_id": ids["Drop the old API?"], "action": "escalate",
                 "answer": ""}]}}
    monkeypatch.setattr(agents, "rule_questions", fake_rule)
    monkeypatch.setattr(pipeline.notify, "send", lambda *a, **k: None)
    asyncio.run(pipeline.process_questions("may"))
    assert "Link or plain text?" in seen["inbox"]
    assert fresh_db.harry_inbox("may") == []
    esc = fresh_db.escalated_questions("may")
    assert [q["question"] for q in esc] == ["Drop the old API?"]
    ans = fresh_db.answers_for("may", "issue#1")
    assert ans[0]["answer"] == "Plain text." and ans[0]["answered_by"] == "Harry"
    # the engineer's prompt carries Harry's ruling as binding
    assert "Plain text." in agents._danny_answers("may", "issue#1")
    # nothing to do → no agent call
    async def boom(*a, **k):
        raise AssertionError("should not be called")
    monkeypatch.setattr(agents, "rule_questions", boom)
    asyncio.run(pipeline.process_questions("may"))


def test_lead_opens_tracking_issues(fresh_db, may, monkeypatch):
    from harness import pipeline, gh
    created = []
    monkeypatch.setattr(gh, "create_issue",
                        lambda repo, t, b: created.append((t, b)) or 41 + len(created))
    pipeline._open_tracking_issues(may, [
        {"title": "Cover policy gates with tests", "body": "Acceptance: ..."},
        {"title": "", "body": "no title"},
        {"title": "Cover policy gates with tests", "body": "dup"}])
    assert len(created) == 1
    item = fresh_db.get_item("may", "issue", 42)
    assert item["status"] == "new" and item["author"] == may["lead_name"]
    # a second plan naming the same open issue does not file it again
    pipeline._open_tracking_issues(may, [
        {"title": "Cover policy-gates with tests!", "body": "again"}])
    assert len(created) == 1
    # the daily cap holds, and the policy can switch it off entirely
    cap = pipeline.TRACKING_ISSUES_PER_DAY
    pipeline._open_tracking_issues(may, [
        {"title": f"Issue {n}", "body": "b"} for n in range(cap)])
    assert len(created) == cap   # 1 + (cap - 1) more = the daily cap
    fresh_db.set_policy("may", "file_issues", "off")
    pipeline._open_tracking_issues(may, [{"title": "Nope", "body": "b"}])
    assert len(created) == cap


def test_reconcile_branches_logs(fresh_db, may, monkeypatch):
    from harness import pipeline, repo
    monkeypatch.setattr(repo, "reconcile_dev", lambda p: "fast-forwarded")
    pipeline._reconcile_branches(may)
    assert any("fast-forwarded" in e["message"]
               for e in fresh_db.recent_events(5, "may"))
    monkeypatch.setattr(repo, "reconcile_dev", lambda p: "diverged")
    pipeline._reconcile_branches(may)
    assert any("diverged" in e["message"] and e["level"] == "warn"
               for e in fresh_db.recent_events(5, "may"))


def test_event_driven_cycle(fresh_db, may, monkeypatch):
    """New items are triaged without a plan; the lead plans only when
    something needs judgement; the lead's fix is the sign-off; fresh
    approvals run ahead of retries; a quiet desk costs no agent runs."""
    import asyncio
    from harness import pipeline, repo, agents
    fixed, planned, triaged = [], [], []
    monkeypatch.setattr(pipeline, "sync", lambda p: None)
    monkeypatch.setattr(pipeline, "_reconcile_branches", lambda p: None)
    monkeypatch.setattr(repo, "clean_checkout", lambda p, b: "/tmp")
    monkeypatch.setattr(repo, "ensure_test_env", lambda p: None)
    async def fake_fix(project, item, persona="Malcolm"):
        fixed.append(item["number"])
    async def fake_triage(project, item):
        triaged.append(item["number"])
        fresh_db.update_item("may", "issue", item["number"], status="triaged",
                             verdict="bug", plan="do x")
    async def fake_plan(project, digest, cwd):
        planned.append(digest)
        return {"ok": True, "output": {"summary": "s", "tasks": [
            {"action": "fix", "kind": "issue", "number": 7, "reason": "go"},
            {"action": "skip", "kind": "issue", "number": 8, "reason": "later"}]}}
    async def no_q(name=None):
        return None
    monkeypatch.setattr(pipeline, "fix_item", fake_fix)
    monkeypatch.setattr(pipeline, "triage_item", fake_triage)
    monkeypatch.setattr(pipeline, "process_questions", no_q)
    monkeypatch.setattr(agents, "lead_plan", fake_plan)
    fresh_db.set_policy("may", "fix_issues", "lead")
    fresh_db.upsert_item("may", "issue", 3, "retry", "a", "open", "x")
    fresh_db.update_item("may", "issue", 3, status="approved", error="tests failed")
    fresh_db.upsert_item("may", "issue", 7, "fresh", "a", "open", "x")
    fresh_db.upsert_item("may", "issue", 8, "meh", "a", "open", "x")

    asyncio.run(pipeline.run_cycle(may))
    assert sorted(triaged) == [7, 8]                 # reflex, no plan needed first
    assert len(planned) == 1 and "sign-off" in planned[0]
    assert "Plan:" in planned[0]                     # the lead sees the whole case
    assert fixed == [7]                              # signed off → engineer; #3 waits
    th = [r["text"] for r in fresh_db.thread("may", "issue#8")]
    assert any("Not this time" in t for t in th)
    assert fresh_db.get_item("may", "issue", 8)["status"] == "triaged"

    # second pass: nothing new — no plan, the retry gets its turn
    fixed.clear()
    fresh_db.update_item("may", "issue", 7, status="queued")
    asyncio.run(pipeline.run_cycle(may))
    assert len(planned) == 1
    assert fixed == [3]
    assert pipeline.work_ready(may) is False         # quiet desk, no spin


def test_unreviewed_pr_merge_runs_the_suite_first(fresh_db, may, monkeypatch):
    """Merge now skips Ruth, never the tests."""
    import asyncio
    from harness import gh, pipeline, repo

    fresh_db.upsert_item("may", "pr", 9, "A contribution", "outsider",
                         "open", "x")
    item = fresh_db.get_item("may", "pr", 9)
    merged = []

    class _NoLock:
        def __enter__(self): return None
        def __exit__(self, *a): return False

    monkeypatch.setattr(repo, "clone_lock", lambda project: _NoLock())
    monkeypatch.setattr(repo, "fetch_pr_branch",
                        lambda project, number, branch: "/tmp")
    monkeypatch.setattr(gh, "pr_detail", lambda repo_, number: {
        "isDraft": False, "baseRefName": "dev"})
    monkeypatch.setattr(gh, "merge_pr",
                        lambda repo_, number, **kw: merged.append(number))

    monkeypatch.setattr(repo, "run_pr_tests",
                        lambda project, number: (False, "2 failed"))
    asyncio.run(pipeline.merge_pr_item(may, item))
    assert merged == []
    after = fresh_db.get_item("may", "pr", 9)
    assert after["status"] == "waiting_human" and "2 failed" in after["error"]

    monkeypatch.setattr(repo, "run_pr_tests",
                        lambda project, number: (True, "ok"))
    asyncio.run(pipeline.merge_pr_item(may, item))
    assert merged == [9]
    assert fresh_db.get_item("may", "pr", 9)["status"] == "queued"


def test_draft_pr_is_never_merged(fresh_db, may, monkeypatch):
    import asyncio
    from harness import gh, pipeline

    fresh_db.upsert_item("may", "pr", 10, "WIP", "outsider", "open", "x")
    item = fresh_db.get_item("may", "pr", 10)
    monkeypatch.setattr(gh, "pr_detail", lambda repo_, number: {"isDraft": True})
    def _no(*a, **k):
        raise AssertionError("merged a draft")
    monkeypatch.setattr(gh, "merge_pr", _no)
    asyncio.run(pipeline.merge_pr_item(may, item))
    assert fresh_db.get_item("may", "pr", 10)["status"] == "waiting_human"


def test_lead_plans_only_when_the_backlog_changes(fresh_db, may):
    """Retries, restart requeues and failed attempts bump updated_at on
    approved items; none of that needs the lead. New approved items do."""
    from harness import pipeline
    fresh_db.set_setting("last_plan_at.may", "2999-01-01T00:00:00Z")
    for n in (1, 2, 3):
        fresh_db.upsert_item("may", "issue", n, f"t{n}", "a", "open", "x")
        fresh_db.update_item("may", "issue", n, status="approved")
    assert any("ordering" in r for r in pipeline.desk_events(may))
    # the lead planned over this backlog
    fresh_db.set_setting("plan_backlog.may", "[1, 2, 3]")
    assert pipeline.desk_events(may) == []
    # a retry / requeue touches the items but does not change the backlog
    fresh_db.update_item("may", "issue", 2, status="working")
    fresh_db.update_item("may", "issue", 2, status="approved")
    assert pipeline.desk_events(may) == []
    # a genuinely new approved item does
    fresh_db.upsert_item("may", "issue", 4, "t4", "a", "open", "x")
    fresh_db.update_item("may", "issue", 4, status="approved")
    assert any("ordering" in r for r in pipeline.desk_events(may))


def test_forced_cycle_does_not_make_the_lead_plan(fresh_db, may, monkeypatch):
    import asyncio
    from harness import pipeline, repo, agents
    planned = []
    async def fake_plan(project, digest, cwd):
        planned.append(1)
        return {"ok": True, "output": {"summary": "", "tasks": []}}
    monkeypatch.setattr(agents, "lead_plan", fake_plan)
    monkeypatch.setattr(pipeline, "sync", lambda p: None)
    monkeypatch.setattr(pipeline, "_reconcile_branches", lambda p: None)
    monkeypatch.setattr(pipeline, "_release_due", lambda p: None)
    monkeypatch.setattr(repo, "clean_checkout", lambda p, b: "/tmp")
    monkeypatch.setattr(repo, "ensure_test_env", lambda p: None)
    fresh_db.set_setting("last_plan_at.may", "2999-01-01T00:00:00Z")
    asyncio.run(pipeline.run_cycle(may, force=True))
    assert planned == []


def test_directions_are_actioned_during_an_engineer_wave(fresh_db, may, monkeypatch):
    """A direction typed while engineers are busy reaches Harry within the
    attendant interval, not after the sweep."""
    import asyncio, time
    from harness import pipeline, repo, agents
    monkeypatch.setattr(pipeline, "ATTEND_INTERVAL_S", 0.05)
    monkeypatch.setattr(pipeline, "sync", lambda p: None)
    monkeypatch.setattr(pipeline, "_reconcile_branches", lambda p: None)
    monkeypatch.setattr(pipeline, "_release_due", lambda p: None)
    monkeypatch.setattr(pipeline, "desk_events", lambda p: [])
    monkeypatch.setattr(repo, "clean_checkout", lambda p, b: "/tmp")
    monkeypatch.setattr(repo, "ensure_test_env", lambda p: None)
    fresh_db.upsert_item("may", "issue", 9, "t", "a", "open", "x")
    fresh_db.update_item("may", "issue", 9, status="approved")
    timeline = []

    async def slow_fix(project, item, persona="Malcolm"):
        # the direction arrives while the engineer is mid-fix
        await asyncio.sleep(0.1)
        fresh_db.add_direction("may", "make the footer blue")
        await asyncio.sleep(0.6)
        timeline.append(("fix_done", time.monotonic()))
        fresh_db.update_item("may", "issue", 9, status="queued")
    monkeypatch.setattr(pipeline, "fix_item", slow_fix)

    async def fake_directive(project, text, item_key, digest):
        timeline.append(("harry", time.monotonic()))
        return {"ok": True, "output": {"actions": [], "reply": "Noted, blue it is."}}
    monkeypatch.setattr(agents, "execute_directive", fake_directive)

    asyncio.run(pipeline.run_cycle(may, force=True))
    kinds = [k for k, _ in timeline]
    assert kinds.index("harry") < kinds.index("fix_done")
    assert fresh_db.pending_directives() == []
    assert any("Noted, blue it is." in e["message"]
               for e in fresh_db.recent_events(10, "may"))


def test_concurrent_directive_processing_actions_each_direction_once(
        fresh_db, may, monkeypatch):
    import asyncio
    from harness import pipeline, agents
    fresh_db.add_direction("may", "paint it blue")
    actioned = []

    async def slow_directive(project, text, item_key, digest):
        await asyncio.sleep(0.1)
        actioned.append(text)
        return {"ok": True, "output": {"actions": [], "reply": "done"}}
    monkeypatch.setattr(agents, "execute_directive", slow_directive)

    async def both():
        await asyncio.gather(pipeline.process_directives(),
                             pipeline.process_directives())
    asyncio.run(both())
    assert actioned == ["paint it blue"]


def test_approving_a_held_item_resets_the_breaker_window(fresh_db, may, client):
    """Two stale failures must not re-hold (and re-page) an item the operator
    has just deliberately approved — the approval says 'try again'."""
    from harness import pipeline
    fresh_db.upsert_item("may", "issue", 9, "t", "a", "open", "x")
    for _ in range(2):
        rid = fresh_db.start_run("may", "ic", "issue#9", "fix", "m", "Malcolm")
        fresh_db.finish_run(rid, False, 0.1, 1, "boom")
    item = fresh_db.get_item("may", "issue", 9)
    assert pipeline._breaker_tripped(may, item) is True     # held, as before
    client.post("/p/may/issue/9/approve")                   # the operator's say-so
    assert fresh_db.consecutive_failures("may", "issue#9") == 0
    assert fresh_db.get_item("may", "issue", 9)["breaker_trips"] == 0
    item = fresh_db.get_item("may", "issue", 9)
    assert pipeline._breaker_tripped(may, item) is False
    assert item["status"] == "approved"
    # a fresh failure after the reset still counts (nudge the clock one
    # second forward: in the test everything happens inside one second)
    rid = fresh_db.start_run("may", "ic", "issue#9", "fix", "m", "Malcolm")
    fresh_db.finish_run(rid, False, 0.1, 1, "boom again")
    with fresh_db.conn() as c:
        c.execute("UPDATE runs SET started_at = '2999-01-01T00:00:01Z' "
                  "WHERE id = ?", (rid,))
    assert fresh_db.consecutive_failures("may", "issue#9") == 1


def _standup_returning(blockers):
    """A fake stand-up that names the given blockers and records the digest
    it was handed, so a test can read what Harry actually saw."""
    seen = []

    async def fake_standup(digest):
        seen.append(digest)
        return {"ok": True, "error": "", "output": {
            "standup_markdown": "# Stand-up", "all_clear": False,
            "desks": [], "blockers": list(blockers), "decisions": [],
            "staffing": [], "directives": [], "question_for_human": ""}}
    return fake_standup, seen


def test_repeated_blocker_comes_back_marked_unchanged(fresh_db, may,
                                                      monkeypatch):
    """The same blocker twice running, with nothing done about it, must
    reach Harry as his own words plus the fact that nothing moved."""
    import asyncio
    from harness import pipeline, agents
    fake, seen = _standup_returning(
        [{"project": "may", "message": "The roan demo has no owner"}])
    monkeypatch.setattr(agents, "standup", fake)
    asyncio.run(pipeline.run_standup(force=True))
    assert "Blockers you named last stand-up" not in seen[0]  # nothing before
    asyncio.run(pipeline.run_standup(force=True))
    assert "Blockers you named last stand-up, with what changed since:" in seen[1]
    assert "- The roan demo has no owner — unchanged: no activity since" \
        in seen[1]
    # and it is counted, so a third stand-up reads as a third telling
    asyncio.run(pipeline.run_standup(force=True))
    assert "[named at 2 stand-ups running]" in seen[2]
    assert "unchanged" in seen[2]


def test_blocker_on_an_item_that_moved_comes_back_changed(fresh_db, may,
                                                          monkeypatch):
    """A blocker naming an item is judged on that item, not on desk noise:
    the item changing status is what makes it 'changed'."""
    import asyncio
    from harness import pipeline, agents
    fresh_db.upsert_item("may", "issue", 7, "gate", "a", "open", "x")
    fresh_db.update_item("may", "issue", 7, status="held")
    fake, seen = _standup_returning(
        [{"project": "may", "message": "#7 is held behind the breaker"}])
    monkeypatch.setattr(agents, "standup", fake)
    asyncio.run(pipeline.run_standup(force=True))
    fresh_db.update_item("may", "issue", 7, status="approved")
    asyncio.run(pipeline.run_standup(force=True))
    assert "changed: issue#7 held → approved" in seen[1]


def test_blocker_that_is_dropped_is_not_carried_further(fresh_db, may,
                                                        monkeypatch):
    """A blocker Harry stops naming is done with: the next digest must not
    keep asking about it."""
    import asyncio
    from harness import pipeline, agents
    fake, seen = _standup_returning(
        [{"project": "may", "message": "Spend is drifting"}])
    monkeypatch.setattr(agents, "standup", fake)
    asyncio.run(pipeline.run_standup(force=True))
    quiet, seen2 = _standup_returning([])
    monkeypatch.setattr(agents, "standup", quiet)
    asyncio.run(pipeline.run_standup(force=True))     # sees it, names nothing
    assert "Spend is drifting" in seen2[0]
    asyncio.run(pipeline.run_standup(force=True))
    assert "Spend is drifting" not in seen2[1]


def test_directive_close_item_closes_a_shipped_issue(fresh_db, may,
                                                     monkeypatch):
    """The close-out verb: an item whose fix already shipped leaves every
    queue, and its issue is closed on GitHub with the reason attached, so
    the next plan does not put an engineer back on finished work."""
    from harness import pipeline, gh
    closed = []
    monkeypatch.setattr(gh, "close_issue",
                        lambda repo, n, comment="": closed.append((repo, n, comment)))
    fresh_db.upsert_item("may", "issue", 302, "Already fixed", "alice",
                         "open", "x")
    fresh_db.update_item("may", "issue", 302, status="waiting_human",
                         error="agent reported success but made no changes — "
                               "needs a human look",
                         session_id="s-1")
    done = pipeline._apply_directive_actions(may, [
        {"action": "close_item", "kind": "issue", "number": 302,
         "reason": "shipped in v0.38.1, commit 64710b0"},
        {"action": "close_item", "kind": "issue", "number": 999},  # skipped
    ])
    item = fresh_db.get_item("may", "issue", 302)
    assert item["status"] == "closed" and item["gh_state"] == "closed"
    assert item["error"] == "" and item["session_id"] == ""
    assert closed == [("example/may", 302,
                       "Closed as already shipped: shipped in v0.38.1, "
                       "commit 64710b0")]
    assert len(done) == 1 and "closed issue#302" in done[0]
    # And it is out of sight: no queue holds it and the lead's digest, which
    # lists every item GitHub still calls open, no longer mentions it.
    assert not fresh_db.items_by_status("may", "new", "approved", "queued",
                                        "waiting_human")
    assert "issue#302" not in pipeline._state_digest(may)


def test_close_item_leaves_pull_requests_on_github_alone(fresh_db, may,
                                                         monkeypatch):
    """Closing a PR item takes it off our board; closing someone else's pull
    request is not ours to do."""
    from harness import pipeline, gh
    calls = []
    monkeypatch.setattr(gh, "close_issue",
                        lambda *a, **k: calls.append(a))
    fresh_db.upsert_item("may", "pr", 12, "Contributor PR", "bob", "open", "x")
    assert pipeline.close_item(may, "pr", 12, "merged by hand")
    item = fresh_db.get_item("may", "pr", 12)
    assert item["status"] == "closed" and item["gh_state"] == "open"
    assert calls == []


def test_close_item_survives_a_failed_github_close(fresh_db, may, monkeypatch):
    """GitHub refusing the close must not strand the item mid-air: the board
    moves on and the operator is told the GitHub side did not take."""
    from harness import pipeline, gh
    from harness.gh import CmdError

    def boom(repo, n, comment=""):
        raise CmdError(["gh", "issue", "close", str(n)], 1, "", "not found")

    monkeypatch.setattr(gh, "close_issue", boom)
    fresh_db.upsert_item("may", "issue", 40, "t", "alice", "open", "x")
    assert pipeline.close_item(may, "issue", 40)
    item = fresh_db.get_item("may", "issue", 40)
    assert item["status"] == "closed" and item["gh_state"] == "open"
    assert any("GitHub close failed" in e["message"]
               for e in fresh_db.recent_events(20, "may"))
    assert not pipeline.close_item(may, "issue", 41)   # unknown item


def test_a_stranded_fix_gets_its_own_warn_event(fresh_db, may, monkeypatch,
                                                tmp_path):
    """When the land fails, the error goes on the item and into the thread;
    if the safety push failed too, that gets a warn event of its own rather
    than being buried in a truncated error. (Issue #64.)"""
    import asyncio
    from harness import agents, gh, pipeline, repo

    fresh_db.upsert_item("may", "issue", 64, "stranded", "a", "open", "x")
    fresh_db.update_item("may", "issue", 64, status="approved", plan="do it")

    monkeypatch.setattr(gh, "issue_detail",
                        lambda repo_, number: {"number": 64, "title": "t",
                                               "body": "b"})
    monkeypatch.setattr(repo, "add_worktree",
                        lambda project, branch: (tmp_path, ""))
    monkeypatch.setattr(repo, "wt_has_changes", lambda project, wt: True)
    monkeypatch.setattr(repo, "run_tests",
                        lambda project, cwd=None, setup=True, scratch=None:
                        (True, "ok"))
    monkeypatch.setattr(repo, "wt_diff",
                        lambda project, wt: ("1 file changed", "diff"))
    monkeypatch.setattr(repo, "wt_commit_all",
                        lambda project, wt, message: None)
    removed = []
    monkeypatch.setattr(repo, "remove_worktree",
                        lambda project, wt: removed.append(wt))
    monkeypatch.setattr(
        repo, "push_worktree_to_dev",
        lambda project, wt, branch: (
            False, f"rebase onto moved dev conflicted — "
                   f"{repo.SAFETY_PUSH_FAILED} (git push -> 1: rejected) — "
                   "the fix exists only in the worktree on this box"))

    async def fake_fix_issue(project, issue, plan, cwd, resume=None,
                             persona="Malcolm", repro_path=""):
        rid = fresh_db.start_run("may", "ic", "issue#64", "fix", "m", persona)
        fresh_db.finish_run(rid, True, 0.1, 1, "fixed it")
        return {"ok": True, "error": "", "session_id": "s",
                "output": {"success": True, "summary": "fixed it",
                           "docs_updated": False, "notes": "",
                           "commit_message": "fix: issue #64 (#64)"}}

    monkeypatch.setattr(agents, "fix_issue", fake_fix_issue)
    asyncio.run(pipeline.fix_item(may, fresh_db.get_item("may", "issue", 64)))

    after = fresh_db.get_item("may", "issue", 64)
    assert after["status"] == "approved"
    assert repo.SAFETY_PUSH_FAILED in after["error"]
    assert removed == []          # the only copy stays where it is
    assert any("did not land" in r["text"]
               for r in fresh_db.thread("may", "issue#64"))
    stranded = [e for e in fresh_db.recent_events(20, "may")
                if "could not be pushed to origin/harness/issue-64"
                in e["message"]]
    assert len(stranded) == 1 and stranded[0]["level"] == "warn"
    assert "only in the worktree on this box" in stranded[0]["message"]


def test_salvaged_work_from_a_previous_attempt_is_named_on_the_thread(
        fresh_db, may, monkeypatch, tmp_path):
    """A retry cuts the branch from dev again, so add_worktree's note about
    where the last attempt's work was kept has to reach the thread — that is
    the only place a human can find the ref. (Issue #63.)"""
    import asyncio
    from harness import agents, gh, pipeline, repo

    fresh_db.upsert_item("may", "issue", 63, "retried", "a", "open", "x")
    fresh_db.update_item("may", "issue", 63, status="approved", plan="do it")

    monkeypatch.setattr(gh, "issue_detail",
                        lambda repo_, number: {"number": 63, "title": "t",
                                               "body": "b"})
    monkeypatch.setattr(
        repo, "add_worktree",
        lambda project, branch: (
            tmp_path, "The previous attempt's commits were preserved on "
                      "harness/issue-63-attempt-1 (abc12345)."))
    monkeypatch.setattr(repo, "wt_has_changes", lambda project, wt: True)
    monkeypatch.setattr(repo, "run_tests",
                        lambda project, cwd=None, setup=True, scratch=None:
                        (True, "ok"))
    monkeypatch.setattr(repo, "wt_diff",
                        lambda project, wt: ("1 file changed", "diff"))
    monkeypatch.setattr(repo, "wt_commit_all",
                        lambda project, wt, message: None)
    monkeypatch.setattr(repo, "remove_worktree", lambda project, wt: None)
    monkeypatch.setattr(repo, "push_worktree_to_dev",
                        lambda project, wt, branch: (True, ""))

    async def fake_fix_issue(project, issue, plan, cwd, resume=None,
                             persona="Malcolm", repro_path=""):
        rid = fresh_db.start_run("may", "ic", "issue#63", "fix", "m", persona)
        fresh_db.finish_run(rid, True, 0.1, 1, "fixed it")
        return {"ok": True, "error": "", "session_id": "s",
                "output": {"success": True, "summary": "fixed it",
                           "docs_updated": False, "notes": "",
                           "commit_message": "fix: issue #63 (#63)"}}

    monkeypatch.setattr(agents, "fix_issue", fake_fix_issue)
    asyncio.run(pipeline.fix_item(may, fresh_db.get_item("may", "issue", 63)))

    assert any("harness/issue-63-attempt-1" in r["text"]
               for r in fresh_db.thread("may", "issue#63"))


def test_tracking_issue_cap_raised_and_names_dropped_title(fresh_db, may, monkeypatch):
    """Issue #65: the daily filing cap was tuned too low (3) and, when it
    bit, the warn event didn't say which tracking issue got dropped — the
    content of the dropped issue was lost. The cap should be 6, and the
    drop event should name the title."""
    from harness import pipeline, gh
    created = []
    monkeypatch.setattr(gh, "create_issue",
                        lambda repo, t, b: created.append((t, b)) or 41 + len(created))
    assert pipeline.TRACKING_ISSUES_PER_DAY == 6

    # fill the cap exactly
    pipeline._open_tracking_issues(may, [
        {"title": f"Issue {n}", "body": "b"}
        for n in range(pipeline.TRACKING_ISSUES_PER_DAY)])
    assert len(created) == pipeline.TRACKING_ISSUES_PER_DAY

    # one more, over the cap: dropped, and the drop is named
    pipeline._open_tracking_issues(may, [
        {"title": "Dropped tracking issue", "body": "b"}])
    assert len(created) == pipeline.TRACKING_ISSUES_PER_DAY  # still not filed

    events = fresh_db.recent_events(5, "may")
    assert any("Dropped tracking issue" in e["message"]
               and "cap (6)" in e["message"]
               for e in events), [e["message"] for e in events]


def test_tracking_issue_cap_keeps_the_first_listed_issue(fresh_db, may,
                                                         monkeypatch):
    """Issue #65: the loop walks new_issues in order and breaks on the cap,
    so when only one slot is left the first-listed issue is the one that
    survives — that ordering is how the desk protects its most important
    filing."""
    from harness import pipeline, gh
    created = []
    monkeypatch.setattr(gh, "create_issue",
                        lambda repo, t, b: created.append((t, b)) or 41 + len(created))
    pipeline._open_tracking_issues(may, [
        {"title": f"Filler {n}", "body": "b"}
        for n in range(pipeline.TRACKING_ISSUES_PER_DAY - 1)])
    assert len(created) == pipeline.TRACKING_ISSUES_PER_DAY - 1

    pipeline._open_tracking_issues(may, [{"title": "Important", "body": "b"},
                                         {"title": "Less so", "body": "b"}])
    assert [t for t, _ in created][-1] == "Important"   # first listed survives
    assert "Less so" not in [t for t, _ in created]
    assert any("Less so" in e["message"]
               for e in fresh_db.recent_events(5, "may"))
