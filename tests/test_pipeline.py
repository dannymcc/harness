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
    monkeypatch.setattr(repo, "add_worktree", lambda project, branch: tmp_path)
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
    monkeypatch.setattr(repo, "add_worktree", lambda project, branch: tmp_path)

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
    pipeline._open_tracking_issues(may, [
        {"title": f"Issue {n}", "body": "b"} for n in range(3)])
    assert len(created) == 3   # 1 + 2 more = cap of 3 per day
    fresh_db.set_policy("may", "file_issues", "off")
    pipeline._open_tracking_issues(may, [{"title": "Nope", "body": "b"}])
    assert len(created) == 3


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


def test_desks_run_concurrently(fresh_db, monkeypatch):
    """A slow desk must not delay the others — the whole point of the
    parallel sweep: a release never queues behind another desk's triage."""
    import asyncio, time
    from harness import pipeline
    for n in ("alpha", "beta", "gamma"):
        fresh_db.create_project(n, f"example/{n}")
    spans = {}

    async def fake_cycle(project, force=False):
        t0 = time.monotonic()
        await asyncio.sleep(0.3)
        spans[project["name"]] = (t0, time.monotonic())
    monkeypatch.setattr(pipeline, "run_cycle", fake_cycle)
    monkeypatch.setattr(pipeline, "work_ready", lambda p: False)
    t0 = time.monotonic()
    asyncio.run(pipeline.run_all_cycles())
    elapsed = time.monotonic() - t0
    assert len(spans) == 3
    assert elapsed < 0.7, f"desks ran serially ({elapsed:.2f}s for 3×0.3s)"
    (a0, a1), (b0, b1) = spans["alpha"], spans["beta"]
    assert a0 < b1 and b0 < a1          # genuinely overlapping


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
