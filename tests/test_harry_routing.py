"""The section decides; the operator gets only what is genuinely theirs.

Anything the section cannot do as it stands — a not-fixable verdict, an
engineer declining, a run that changed nothing, two red runs, a review that
is not an auto-merge — is held for Harry's ruling, and his ruling moves the
item. Harry's own stand-up question goes to the operator once per thing,
on the record of that thing, and never without a reason it is theirs.
"""
import asyncio
from contextlib import contextmanager


# --- helpers -------------------------------------------------------------------

STANDUP_OUT = {"standup_markdown": "# Stand-up", "all_clear": True,
               "desks": [], "blockers": [], "decisions": [],
               "staffing": [], "directives": []}


def _standup(monkeypatch, question, reason="product direction", options=None):
    from harness import agents, pipeline
    out = dict(STANDUP_OUT, question_for_human=question,
               outside_remit_reason=reason,
               question_options=options or ["Yes", "No"])

    async def fake_standup(digest):
        fake_standup.digest = digest
        return {"ok": True, "error": "", "output": out}
    monkeypatch.setattr(agents, "standup", fake_standup)
    asyncio.run(pipeline.run_standup(force=True))
    return fake_standup


def _harry_rules(monkeypatch, decisions, paged=None):
    from harness import agents, pipeline
    sink = paged if paged is not None else []
    monkeypatch.setattr(pipeline.notify, "send",
                        lambda *a, **k: sink.append(a))

    async def fake_rule(inbox, ctx):
        return {"ok": True, "error": "", "output": {"summary": "",
                                                    "decisions": decisions}}
    monkeypatch.setattr(agents, "rule_questions", fake_rule)
    asyncio.run(pipeline.process_questions("may"))


def _fake_engineer(monkeypatch, tmp_path, number, *, success=True,
                   changes=True, tests_pass=True, notes="too risky"):
    """fix_item with the agent and git faked; returns the resume log."""
    from harness import agents, gh, repo
    seen = []
    monkeypatch.setattr(gh, "issue_detail",
                        lambda repo_, n: {"number": n, "title": "t", "body": "b"})
    monkeypatch.setattr(repo, "add_worktree",
                        lambda project, branch, resuming=False: (tmp_path, ""))
    monkeypatch.setattr(repo, "wt_has_changes", lambda project, wt: changes)
    monkeypatch.setattr(repo, "run_tests",
                        lambda project, cwd=None, setup=True, scratch=None:
                        (tests_pass, "ok" if tests_pass else "1 failed"))
    monkeypatch.setattr(repo, "wt_diff", lambda project, wt: ("stat", "diff"))
    monkeypatch.setattr(repo, "wt_commit_all", lambda project, wt, m: None)
    monkeypatch.setattr(repo, "remove_worktree", lambda project, wt: None)
    monkeypatch.setattr(repo, "push_worktree_to_dev",
                        lambda project, wt, branch: (True, ""))

    async def fake_fix_issue(project, issue, plan, cwd, resume=None,
                             persona="Malcolm", repro_path="",
                             worktree_note=""):
        from harness import db
        seen.append(resume)
        rid = db.start_run("may", "ic", f"issue#{number}", "fix", "m", persona)
        db.finish_run(rid, True, 0.1, 1, "ran")
        return {"ok": True, "error": "", "session_id": "sess-1",
                "output": {"success": success, "summary": "did a thing",
                           "docs_updated": False, "notes": notes,
                           "commit_message": f"fix: thing (#{number})"}}
    monkeypatch.setattr(agents, "fix_issue", fake_fix_issue)
    return seen


def _held_question(fresh_db, key):
    return [q for q in fresh_db.harry_inbox("may") if q["item_key"] == key][0]


# --- 1. Harry's stand-up question ------------------------------------------------

def test_standup_question_is_filed_on_the_item_it_names(fresh_db, may,
                                                        monkeypatch):
    """The question lands with the project and item key, so the operator's
    answer routes and moves the item rather than vanishing."""
    from harness import pipeline
    fresh_db.upsert_item("may", "issue", 12, "Dark mode", "a", "open", "x")
    fresh_db.update_item("may", "issue", 12, status="waiting_human")
    _standup(monkeypatch, "Do we take on issue#12 (dark mode) for may?",
             options=["Fix", "Skip"])
    esc = fresh_db.escalated_questions()
    assert len(esc) == 1
    assert (esc[0]["project"], esc[0]["item_key"]) == ("may", "issue#12")
    fresh_db.answer_question(esc[0]["id"], "Fix")
    assert pipeline.route_answers(may) == ["issue#12 -> approved"]


def test_standup_question_naming_several_items_files_once(fresh_db, may,
                                                          monkeypatch):
    for n in (1, 2, 3):
        fresh_db.upsert_item("may", "issue", n, f"feature {n}", "a", "open", "x")
    _standup(monkeypatch, "may has #1, #2 and #3 waiting on you — which first?")
    esc = fresh_db.escalated_questions()
    assert len(esc) == 1
    assert esc[0]["item_key"] == "issue#1"
    assert "Also concerns: issue#2, issue#3" in esc[0]["question"]


def test_standup_question_target_reads_the_desk_and_the_numbers(fresh_db, may):
    from harness import pipeline
    fresh_db.create_project("roan", "example/roan")
    fresh_db.upsert_item("roan", "issue", 5, "x", "a", "open", "x")
    fresh_db.upsert_item("may", "issue", 5, "y", "a", "open", "x")
    # the desk named wins over a bare number that exists on both
    assert pipeline.standup_question_target("roan: is #5 worth doing?") \
        == ("roan", "issue#5", [])
    # a desk with no numbers is still a target; nothing named is the section
    assert pipeline.standup_question_target("Should roan keep two engineers?") \
        == ("roan", "", [])
    assert pipeline.standup_question_target("Do we raise the budget?") \
        == ("", "", [])


def test_standup_question_is_not_repeated_while_one_is_open(fresh_db, may,
                                                            monkeypatch):
    """Rephrasing every hour is the same question: with one already in
    front of the operator about that item, nothing is filed."""
    fresh_db.upsert_item("may", "issue", 12, "Dark mode", "a", "open", "x")
    _standup(monkeypatch, "Do we take on issue#12 for may?")
    _standup(monkeypatch, "Still need your call on may issue#12 — yes or no?")
    _standup(monkeypatch, "may #12: fix it or drop it?")
    assert len(fresh_db.escalated_questions()) == 1
    assert any("already with" in e["message"] and "issue#12" in e["message"]
               for e in fresh_db.recent_events(10))


def test_standup_question_is_not_repeated_over_a_fresh_ruling(fresh_db, may,
                                                              monkeypatch):
    """Answered within the day: not asked again, the event says what the
    ruling was, and the next digest carries the ruling back to Harry."""
    fresh_db.upsert_item("may", "issue", 12, "Dark mode", "a", "open", "x")
    _standup(monkeypatch, "Do we take on issue#12 for may?")
    qid = fresh_db.escalated_questions()[0]["id"]
    fresh_db.answer_question(qid, "Skip — not this quarter")
    fake = _standup(monkeypatch, "may issue#12 — are we doing this or not?")
    assert fresh_db.escalated_questions() == []
    assert any("already ruled on issue#12" in e["message"]
               and "Skip — not this quarter" in e["message"]
               for e in fresh_db.recent_events(10))
    assert "answers to your own questions" in fake.digest
    assert "Skip — not this quarter" in fake.digest


def test_itemless_standup_question_dedupes_on_the_desk(fresh_db, may,
                                                       monkeypatch):
    _standup(monkeypatch, "Should may get a second engineer for the month?")
    _standup(monkeypatch, "may — do we staff up?")
    assert len(fresh_db.escalated_questions()) == 1
    assert fresh_db.escalated_questions()[0]["project"] == "may"
    # a different desk is a different question
    fresh_db.create_project("roan", "example/roan")
    _standup(monkeypatch, "roan — do we staff up?")
    assert len(fresh_db.escalated_questions()) == 2


def test_standup_question_without_a_remit_reason_is_dropped(fresh_db, may,
                                                            monkeypatch):
    """A call Harry could have made is a directive he did not issue."""
    _standup(monkeypatch, "Which of may's four features first?", reason="")
    assert fresh_db.escalated_questions() == []
    assert any("without saying why" in e["message"]
               for e in fresh_db.recent_events(10))


# --- 2. held for Harry, not parked for the operator -------------------------------

@contextmanager
def _nolock(project):
    yield


def _triage(monkeypatch, fresh_db, number, **verdict):
    from harness import agents, gh, pipeline, repo
    out = dict(verdict="feature", valid=True, fixable=False,
               summary="a big feature", plan="", draft_comment="")
    out.update(verdict)
    monkeypatch.setattr(gh, "issue_detail",
                        lambda repo_, n: {"number": n, "title": "t", "body": "b"})
    monkeypatch.setattr(repo, "clone_lock", _nolock)
    monkeypatch.setattr(repo, "clean_checkout", lambda p, b: "/tmp")

    async def fake_triage(project, detail, cwd):
        return {"ok": True, "error": "", "output": out}
    monkeypatch.setattr(agents, "triage_issue", fake_triage)
    fresh_db.upsert_item("may", "issue", number, "big one", "a", "open", "x")
    asyncio.run(pipeline.triage_item(may_project(fresh_db),
                                     fresh_db.get_item("may", "issue", number)))


def may_project(fresh_db):
    return fresh_db.get_project("may")


def test_not_fixable_verdict_is_held_for_harry(fresh_db, may, monkeypatch):
    from harness import pipeline
    _triage(monkeypatch, fresh_db, 21)
    item = fresh_db.get_item("may", "issue", 21)
    assert item["status"] == "held" and item["breaker_trips"] == 1
    q = _held_question(fresh_db, "issue#21")
    assert q["asked_by"] == "Ruth"
    assert fresh_db.question_options(q) == ["Fix", "Skip", "Won't fix"]
    assert "a big feature" in q["question"]
    # held is not work: no fast re-wake, no dispatch
    fresh_db.set_setting("last_plan_at.may", "2999-01-01T00:00:00Z")
    assert pipeline.work_ready(may) is False


def test_needs_operator_verdict_goes_to_the_operator(fresh_db, may,
                                                     monkeypatch):
    _triage(monkeypatch, fresh_db, 22, needs_operator=True)
    assert fresh_db.get_item("may", "issue", 22)["status"] == "waiting_human"
    assert fresh_db.harry_inbox("may") == []


def test_harrys_fix_ruling_moves_a_held_item(fresh_db, may, monkeypatch):
    """His ruling is acted on there and then — and keeps the trip, so the
    next hold on this item is the operator's."""
    _triage(monkeypatch, fresh_db, 23)
    q = _held_question(fresh_db, "issue#23")
    _harry_rules(monkeypatch, [{"question_id": q["id"], "action": "answer",
                                "answer": "Fix"}])
    item = fresh_db.get_item("may", "issue", 23)
    assert item["status"] == "approved" and item["error"] == ""
    assert item["breaker_trips"] == 1
    assert any("Harry's ruling moved issue#23" in e["message"]
               for e in fresh_db.recent_events(10, "may"))


def test_harrys_skip_and_wont_fix_rulings(fresh_db, may, monkeypatch):
    from harness import pipeline
    _triage(monkeypatch, fresh_db, 24)
    _triage(monkeypatch, fresh_db, 25)
    q24 = _held_question(fresh_db, "issue#24")
    q25 = _held_question(fresh_db, "issue#25")
    _harry_rules(monkeypatch, [
        {"question_id": q24["id"], "action": "answer", "answer": "Skip"},
        {"question_id": q25["id"], "action": "answer", "answer": "Won't fix"}])
    parked = fresh_db.get_item("may", "issue", 24)
    assert parked["status"] == "waiting_human"
    assert parked["error"].startswith("parked by Harry")
    assert fresh_db.get_item("may", "issue", 25)["status"] == "rejected"
    # the digest does not describe his own parking as the operator's blocker
    digest = pipeline._standup_digest()
    assert "parked by your own ruling: issue#24" in digest
    assert "waiting on operator" not in digest


def test_harrys_fix_under_an_approve_policy_is_a_recommendation(fresh_db, may,
                                                                monkeypatch):
    """fix_issues: approve makes the start the operator's click; Harry's
    "fix" on an item nobody has signed off goes to them, with his view."""
    fresh_db.set_policy("may", "fix_issues", "approve")
    _triage(monkeypatch, fresh_db, 26)
    q = _held_question(fresh_db, "issue#26")
    _harry_rules(monkeypatch, [{"question_id": q["id"], "action": "answer",
                                "answer": "Fix"}])
    item = fresh_db.get_item("may", "issue", 26)
    assert item["status"] == "waiting_human"
    assert item["error"].startswith("Harry recommends Fix")


def test_harry_escalating_a_held_item_parks_it_for_the_operator(fresh_db, may,
                                                                monkeypatch):
    _triage(monkeypatch, fresh_db, 27)
    q = _held_question(fresh_db, "issue#27")
    _harry_rules(monkeypatch, [{"question_id": q["id"], "action": "escalate",
                                "answer": "", "outside_remit_reason":
                                "changes the pricing page"}])
    item = fresh_db.get_item("may", "issue", 27)
    assert item["status"] == "waiting_human"
    assert "changes the pricing page" in item["error"]
    assert len(fresh_db.escalated_questions("may")) == 1
    assert any("changes the pricing page" in e["message"]
               for e in fresh_db.recent_events(10, "may"))


def test_operator_can_still_answer_a_held_items_question(fresh_db, may,
                                                          monkeypatch):
    """Their answer over Harry's head moves the item and forgives the trip."""
    from harness import pipeline
    _triage(monkeypatch, fresh_db, 28)
    q = _held_question(fresh_db, "issue#28")
    fresh_db.answer_question(q["id"], "Fix")
    assert pipeline.route_answers(may) == ["issue#28 -> approved"]
    assert fresh_db.get_item("may", "issue", 28)["breaker_trips"] == 0


def test_harrys_ruling_still_does_not_move_an_unheld_item(fresh_db, may):
    """Only a held item is his to move: on anything else his answer is a
    ruling for the asker, not the operator's sign-off."""
    from harness import pipeline
    fresh_db.upsert_item("may", "issue", 29, "x", "a", "open", "x")
    fresh_db.update_item("may", "issue", 29, status="waiting_human")
    fresh_db.ask_question("may", "Ruth", "issue#29", "Fix or leave?",
                          options=["Fix", "Skip"])
    q = fresh_db.harry_inbox("may")[0]
    fresh_db.answer_question(q["id"], "Fix", by="Harry")
    assert pipeline.route_answers(may) == []
    assert fresh_db.get_item("may", "issue", 29)["status"] == "waiting_human"


def test_review_that_is_not_an_auto_merge_is_held_for_harry(fresh_db, may,
                                                             monkeypatch):
    from harness import agents, gh, pipeline, repo
    monkeypatch.setattr(gh, "pr_detail",
                        lambda repo_, n: {"isDraft": False, "number": n})
    monkeypatch.setattr(gh, "pr_diff", lambda repo_, n: "diff")
    monkeypatch.setattr(repo, "fetch_pr_branch", lambda p, n, b: "/tmp")
    monkeypatch.setattr(repo, "remove_pr_run", lambda p, n: None)
    monkeypatch.setattr(repo, "run_pr_tests", lambda p, n: (True, "ok"))

    async def fake_review(project, detail, diff, tests, cwd):
        return {"ok": True, "error": "", "output": {
            "verdict": "merge", "valuable": True, "summary": "tidy",
            "risks": "", "draft_review": ""}}
    monkeypatch.setattr(agents, "review_pr", fake_review)
    fresh_db.upsert_item("may", "pr", 40, "a pr", "bob", "open", "x")
    asyncio.run(pipeline.review_item(may, fresh_db.get_item("may", "pr", 40)))
    assert fresh_db.get_item("may", "pr", 40)["status"] == "held"
    q = _held_question(fresh_db, "pr#40")
    assert fresh_db.question_options(q) == ["Merge", "Skip", "Won't fix"]
    # merge_prs defaults to approve, so his "merge" is the operator's press
    _harry_rules(monkeypatch, [{"question_id": q["id"], "action": "answer",
                                "answer": "Merge"}])
    pr = fresh_db.get_item("may", "pr", 40)
    assert pr["status"] == "waiting_human"
    assert pr["error"].startswith("Harry recommends Merge")


# --- 3. the refusal loop -----------------------------------------------------------

def test_engineer_decline_is_held_then_the_operators_after_harrys_ruling(
        fresh_db, may, monkeypatch, tmp_path):
    """Decline → held for Harry. His "fix" buys one more go; a second decline
    goes to the operator instead of round again. Their approve resets it."""
    from harness import pipeline
    paged = []
    monkeypatch.setattr(pipeline.notify, "send",
                        lambda *a, **k: paged.append(a))
    fresh_db.upsert_item("may", "issue", 50, "refused", "a", "open", "x")
    fresh_db.update_item("may", "issue", 50, status="approved", plan="p")
    _fake_engineer(monkeypatch, tmp_path, 50, success=False,
                   notes="closed-and-held, taken out of rotation")

    asyncio.run(pipeline.fix_item(may, fresh_db.get_item("may", "issue", 50)))
    item = fresh_db.get_item("may", "issue", 50)
    assert item["status"] == "held" and item["breaker_trips"] == 1
    assert paged == []
    q = _held_question(fresh_db, "issue#50")
    assert q["asked_by"] == "Malcolm" and "taken out of rotation" in q["question"]
    fresh_db.set_setting("last_plan_at.may", "2999-01-01T00:00:00Z")
    assert pipeline.work_ready(may) is False

    _harry_rules(monkeypatch, [{"question_id": q["id"], "action": "answer",
                                "answer": "Fix"}], paged)
    assert fresh_db.get_item("may", "issue", 50)["status"] == "approved"

    asyncio.run(pipeline.fix_item(may, fresh_db.get_item("may", "issue", 50)))
    item = fresh_db.get_item("may", "issue", 50)
    assert item["status"] == "waiting_human" and item["breaker_trips"] == 2
    assert "after Harry's ruling" in item["error"]
    assert len(paged) == 1
    assert len(fresh_db.harry_inbox("may")) == 0    # nothing more to rule on

    # the operator's approve is a fresh start for the trip count — but the
    # engineer refusing in the very same words is the question Harry has
    # already answered this week, so it is not put to him a third time: it
    # goes back to the operator saying so, not round the loop again
    fresh_db.update_item("may", "issue", 50, status="approved", error="",
                         breaker_reset_at=fresh_db.now(), breaker_trips=0)
    asyncio.run(pipeline.fix_item(may, fresh_db.get_item("may", "issue", 50)))
    item = fresh_db.get_item("may", "issue", 50)
    assert item["status"] == "waiting_human"
    assert "Harry has already ruled on this once" in item["error"]
    assert len(fresh_db.harry_inbox("may")) == 0
    # a refusal for a new reason is a new question, and Harry's again
    fresh_db.update_item("may", "issue", 50, status="approved", error="",
                         breaker_reset_at=fresh_db.now(), breaker_trips=0)
    _fake_engineer(monkeypatch, tmp_path, 50, success=False,
                   notes="the spec contradicts the README")
    asyncio.run(pipeline.fix_item(may, fresh_db.get_item("may", "issue", 50)))
    assert fresh_db.get_item("may", "issue", 50)["status"] == "held"


def test_no_change_run_is_held_for_harry(fresh_db, may, monkeypatch, tmp_path):
    from harness import pipeline
    fresh_db.upsert_item("may", "issue", 51, "nothing", "a", "open", "x")
    fresh_db.update_item("may", "issue", 51, status="approved", plan="p")
    _fake_engineer(monkeypatch, tmp_path, 51, changes=False)
    asyncio.run(pipeline.fix_item(may, fresh_db.get_item("may", "issue", 51)))
    item = fresh_db.get_item("may", "issue", 51)
    assert item["status"] == "held"
    assert "changed nothing" in item["error"]
    assert _held_question(fresh_db, "issue#51")["asked_by"] == "Malcolm"


def test_second_red_run_is_held_for_harry(fresh_db, may, monkeypatch, tmp_path):
    from harness import pipeline
    fresh_db.upsert_item("may", "issue", 52, "red", "a", "open", "x")
    fresh_db.update_item("may", "issue", 52, status="approved", plan="p")
    _fake_engineer(monkeypatch, tmp_path, 52, tests_pass=False)
    asyncio.run(pipeline.fix_item(may, fresh_db.get_item("may", "issue", 52)))
    assert fresh_db.get_item("may", "issue", 52)["status"] == "approved"
    asyncio.run(pipeline.fix_item(may, fresh_db.get_item("may", "issue", 52)))
    item = fresh_db.get_item("may", "issue", 52)
    assert item["status"] == "held"
    q = _held_question(fresh_db, "issue#52")
    assert "1 failed" in q["question"]


def test_landing_forgives_the_trips(fresh_db, may, monkeypatch, tmp_path):
    from harness import pipeline
    fresh_db.upsert_item("may", "issue", 53, "ok", "a", "open", "x")
    fresh_db.update_item("may", "issue", 53, status="approved", plan="p",
                         breaker_trips=1)
    _fake_engineer(monkeypatch, tmp_path, 53)
    asyncio.run(pipeline.fix_item(may, fresh_db.get_item("may", "issue", 53)))
    item = fresh_db.get_item("may", "issue", 53)
    assert item["status"] == "queued" and item["breaker_trips"] == 0


def test_the_same_hold_question_already_ruled_on_goes_to_the_operator(
        fresh_db, may, monkeypatch):
    """Word-for-word the same question Harry answered this week is not put
    to him again — and the item must not sit held with nobody asked."""
    from harness import pipeline
    monkeypatch.setattr(pipeline.notify, "send", lambda *a, **k: None)
    fresh_db.upsert_item("may", "issue", 54, "same", "a", "open", "x")
    item = fresh_db.get_item("may", "issue", 54)
    assert pipeline.hold_item(may, item, "Ruth", "not fixable", "ctx") == "held"
    q = _held_question(fresh_db, "issue#54")
    fresh_db.answer_question(q["id"], "Skip", by="Harry")
    fresh_db.update_item("may", "issue", 54, status="new", breaker_trips=0)
    item = fresh_db.get_item("may", "issue", 54)
    assert pipeline.hold_item(may, item, "Ruth", "not fixable", "ctx") \
        == "waiting_human"


# --- 4. the stand-up digest ----------------------------------------------------------

def test_digest_does_not_call_the_operators_items_harrys_blockers(fresh_db, may):
    from harness import config, pipeline
    fresh_db.upsert_item("may", "issue", 60, "theirs", "a", "open", "x")
    fresh_db.update_item("may", "issue", 60, status="waiting_human",
                         verdict="feature")
    fresh_db.upsert_item("may", "issue", 61, "his", "a", "open", "x")
    fresh_db.update_item("may", "issue", 61, status="held",
                         error="Ruth's verdict is feature — with Harry")
    digest = pipeline._standup_digest()
    assert "waiting on operator" not in digest
    assert f"with {config.OPERATOR} (their call, not a blocker of yours): " \
           "issue#60" in digest
    assert "HELD (yours to rule on) issue#61" in digest
