"""What happens after the operator answers a question.

An answer is an instruction about the item it is about: it moves the item,
it wakes the desk, and it is not asked again while it stands.
"""
import asyncio


def _asked(fresh_db, item_key="issue#5", question="Fix this or leave it?"):
    fresh_db.ask_question("may", "Ruth", item_key, question,
                          options=["Fix", "Skip"])
    return fresh_db.open_questions("may")[0]


def test_answer_wording_maps_to_a_fixed_set_of_actions(fresh_db):
    """The mapping is a table, not a judgement — and it only fires on
    wording that says what to do."""
    assert fresh_db.answer_action("Fix") == "proceed"
    assert fresh_db.answer_action("fix it.") == "proceed"
    assert fresh_db.answer_action("Go ahead") == "proceed"
    assert fresh_db.answer_action("Skip") == "hold"
    assert fresh_db.answer_action("Won't fix") == "reject"
    assert fresh_db.answer_action("wont fix") == "reject"
    # a bare yes answers the question, not the item's fate
    assert fresh_db.answer_action("Yes") == ""
    assert fresh_db.answer_action("The intended behaviour is X") == ""


def test_fix_answer_puts_the_issue_back_in_the_flow(fresh_db, may, monkeypatch):
    """(a) answering "Fix" on an item parked for a human gets it worked on
    the next cycle, even under fix_issues: approve — the operator saying so
    is the sign-off."""
    from harness import agents, pipeline, repo
    fresh_db.set_policy("may", "fix_issues", "approve")
    fresh_db.upsert_item("may", "issue", 5, "A bug", "alice", "open", "x")
    fresh_db.update_item("may", "issue", 5, status="waiting_human",
                         error="not fixable on the evidence")
    q = _asked(fresh_db)
    fresh_db.answer_question(q["id"], "Fix")

    assert pipeline.route_answers(may) == ["issue#5 -> approved"]
    item = fresh_db.get_item("may", "issue", 5)
    assert item["status"] == "approved" and item["error"] == ""

    fixed = []
    monkeypatch.setattr(pipeline, "sync", lambda p: None)
    monkeypatch.setattr(pipeline, "_reconcile_branches", lambda p: None)
    monkeypatch.setattr(repo, "clean_checkout", lambda p, b: "/tmp")
    monkeypatch.setattr(repo, "ensure_test_env", lambda p: None)

    async def fake_fix(project, item, persona="Malcolm"):
        fixed.append(item["number"])

    async def fake_plan(project, digest, cwd):
        return {"ok": True, "output": {"summary": "s", "tasks": []}}

    async def no_q(name=None):
        return None

    monkeypatch.setattr(pipeline, "fix_item", fake_fix)
    monkeypatch.setattr(pipeline, "process_questions", no_q)
    monkeypatch.setattr(agents, "lead_plan", fake_plan)
    asyncio.run(pipeline.run_cycle(may))
    assert fixed == [5]


def test_answer_through_the_gui_moves_the_item_on_the_click(client, fresh_db):
    """(b) the answer is not filed and silently discarded: the item moves
    while the operator is still looking at the page."""
    fresh_db.upsert_item("may", "issue", 6, "A bug", "alice", "open", "x")
    fresh_db.update_item("may", "issue", 6, status="waiting_human")
    q = _asked(fresh_db, "issue#6")
    r = client.post(f"/p/may/question/{q['id']}/answer",
                    data={"answer": "Fix"}, follow_redirects=False)
    assert r.status_code == 303
    assert fresh_db.get_item("may", "issue", 6)["status"] == "approved"
    assert any("moved issue#6" in e["message"]
               for e in fresh_db.recent_events(10, "may"))


def test_hold_and_reject_answers_are_recorded_not_worked(fresh_db, may):
    from harness import pipeline
    for number, answer, expected in ((7, "Skip", "waiting_human"),
                                     (8, "Won't fix", "rejected")):
        fresh_db.upsert_item("may", "issue", number, "A bug", "a", "open", "x")
        fresh_db.update_item("may", "issue", number, status="triaged",
                             error="two red runs")
        q = _asked(fresh_db, f"issue#{number}")
        fresh_db.answer_question(q["id"], answer)
        pipeline.route_answers(may)
        moved = fresh_db.get_item("may", "issue", number)
        assert moved["status"] == expected
        # not going back to an agent, so why it stopped stays on the record
        assert moved["error"] == "two red runs"
        text = "\n".join(r["text"] for r in fresh_db.thread("may", f"issue#{number}"))
        assert answer in text and expected in text


def test_an_answer_that_says_nothing_goes_back_to_the_agent_that_asked(
        fresh_db, may):
    """Free text is a message, not a state change — but the item still has
    to go somewhere rather than sit in waiting_human forever."""
    from harness import pipeline
    # an engineer had already started: they resume with the answer
    fresh_db.upsert_item("may", "issue", 9, "A bug", "a", "open", "x")
    fresh_db.update_item("may", "issue", 9, status="waiting_human",
                         session_id="sess-1", branch="harness/issue-9")
    q = _asked(fresh_db, "issue#9")
    fresh_db.answer_question(q["id"], "It should round half up, like the docs say")
    pipeline.route_answers(may)
    assert fresh_db.get_item("may", "issue", 9)["status"] == "approved"
    # nobody has started: it goes back through triage, which reads the thread
    fresh_db.upsert_item("may", "issue", 10, "A bug", "a", "open", "x")
    fresh_db.update_item("may", "issue", 10, status="waiting_human")
    q = _asked(fresh_db, "issue#10")
    fresh_db.answer_question(q["id"], "It should round half up, like the docs say")
    pipeline.route_answers(may)
    assert fresh_db.get_item("may", "issue", 10)["status"] == "new"


def test_the_same_question_is_not_put_again_over_a_live_answer(fresh_db, may):
    """(c) re-asking is refused, and the answer already given is in front of
    the agent that would have asked."""
    from harness import agents
    fresh_db.upsert_item("may", "issue", 11, "A bug", "a", "open", "x")
    q = _asked(fresh_db, "issue#11", "Fix this or leave it?")
    fresh_db.answer_question(q["id"], "Fix")
    # same question, different punctuation and case: still the same question
    assert fresh_db.ask_question("may", "Ruth", "issue#11",
                                 "fix this or leave it?") is None
    assert len(fresh_db.answers_for("may", "issue#11")) == 1
    assert not fresh_db.open_questions("may")
    context = agents._item_context("may", "issue#11")
    assert "Fix this or leave it?" in context and "A: Fix" in context
    # a different question about the same item still gets through
    assert fresh_db.ask_question("may", "Ruth", "issue#11",
                                 "Which branch should this land on?")
    # and so does the same question once the answer is stale
    old = fresh_db._days_ago(fresh_db.ANSWER_DEDUP_DAYS + 1)
    with fresh_db.conn() as c:
        c.execute("UPDATE questions SET answered_at = ? WHERE id = ?",
                  (old, q["id"]))
    assert fresh_db.ask_question("may", "Ruth", "issue#11",
                                 "Fix this or leave it?")


def test_an_answer_is_a_desk_event(fresh_db, may):
    from harness import pipeline
    fresh_db.set_setting("last_plan_at.may", "2999-01-01T00:00:00Z")
    fresh_db.upsert_item("may", "issue", 12, "A bug", "a", "open", "x")
    fresh_db.update_item("may", "issue", 12, status="waiting_human")
    assert pipeline.desk_events(may) == []       # nothing to decide
    q = _asked(fresh_db, "issue#12")
    fresh_db.answer_question(q["id"], "Fix")
    fresh_db.set_setting("last_plan_at.may", "2000-01-01T00:00:00Z")
    assert any("answered a question" in r for r in pipeline.desk_events(may))
    assert pipeline.work_ready(may) is True
    # Harry ruling on his own people is not news to the desk: he answers
    # them constantly and his answer reaches them by itself
    q2 = _asked(fresh_db, "issue#12", "Which module owns this?")
    fresh_db.answer_question(q2["id"], "The parser", by="Harry")
    assert [r["id"] for r in fresh_db.answers_since("may")] == [q["id"]]


def test_harrys_ruling_does_not_sign_an_item_off(fresh_db, may):
    """waiting_human means waiting on the operator; Harry answering is not
    the operator's click, so it must not start an engineer."""
    from harness import pipeline
    fresh_db.upsert_item("may", "issue", 13, "A bug", "a", "open", "x")
    fresh_db.update_item("may", "issue", 13, status="waiting_human")
    q = _asked(fresh_db, "issue#13")
    fresh_db.answer_question(q["id"], "Fix", by="Harry")
    assert pipeline.route_answers(may) == []
    assert fresh_db.get_item("may", "issue", 13)["status"] == "waiting_human"


def test_answers_stranded_before_this_existed_are_picked_up_once(fresh_db, may):
    """Rows written before routed_at read as unrouted, so the items they
    stranded re-enter the flow on the first cycle — and only that once."""
    from harness import pipeline
    fresh_db.upsert_item("may", "issue", 14, "A bug", "a", "open", "x")
    fresh_db.update_item("may", "issue", 14, status="waiting_human")
    q = _asked(fresh_db, "issue#14")
    fresh_db.answer_question(q["id"], "Fix")
    with fresh_db.conn() as c:      # as the old code left it
        c.execute("UPDATE questions SET routed_at = '' WHERE id = ?", (q["id"],))
    assert pipeline.route_answers(may) == ["issue#14 -> approved"]
    # the operator parks it again by hand: the same old answer must not
    # drag it back out
    fresh_db.update_item("may", "issue", 14, status="waiting_human")
    assert pipeline.route_answers(may) == []
    assert fresh_db.get_item("may", "issue", 14)["status"] == "waiting_human"


def test_a_fix_answer_forgives_the_items_failure_history(fresh_db, may):
    """Held after repeated failures and told to fix it anyway: the breaker
    must not hold it again before the fresh attempt has run."""
    from harness import pipeline
    fresh_db.upsert_item("may", "issue", 17, "A bug", "a", "open", "x")
    for _ in range(pipeline.BREAKER_THRESHOLD):
        rid = fresh_db.start_run("may", "ic", "issue#17", "fix", "m", "Malcolm")
        fresh_db.finish_run(rid, False, 0.1, 1, "boom")
    fresh_db.update_item("may", "issue", 17, status="waiting_human",
                         breaker_trips=pipeline.MAX_BREAKER_TRIPS)
    q = _asked(fresh_db, "issue#17")
    fresh_db.answer_question(q["id"], "Fix")
    pipeline.route_answers(may)
    item = fresh_db.get_item("may", "issue", 17)
    assert item["status"] == "approved" and item["breaker_trips"] == 0
    assert fresh_db.consecutive_failures("may", "issue#17") == 0
    assert pipeline._breaker_tripped(may, item) is False


def test_a_breaker_question_is_left_to_the_breakers_own_ruling(fresh_db, may):
    """Held items have their own vocabulary (retry/split/escalate); routing
    must not second-guess it."""
    from harness import pipeline
    fresh_db.upsert_item("may", "issue", 18, "A bug", "a", "open", "x")
    fresh_db.update_item("may", "issue", 18, status="held")
    fresh_db.ask_question("may", pipeline.BREAKER_ASKER, "issue#18",
                          "issue#18 has failed twice. Rule on it.",
                          options=pipeline.BREAKER_OPTIONS)
    q = fresh_db.open_questions("may")[0]
    fresh_db.answer_question(q["id"], "retry")
    assert pipeline.route_answers(may) == []
    assert fresh_db.get_item("may", "issue", 18)["status"] == "held"


def test_answers_about_items_in_hand_or_gone_change_nothing(fresh_db, may):
    """An item an engineer is on, or one already closed on GitHub, is not
    shunted about by an answer — it reaches them through the thread."""
    from harness import pipeline
    fresh_db.upsert_item("may", "issue", 15, "A bug", "a", "open", "x")
    fresh_db.update_item("may", "issue", 15, status="working")
    q = _asked(fresh_db, "issue#15")
    fresh_db.answer_question(q["id"], "Fix")
    assert pipeline.route_answers(may) == []
    assert fresh_db.get_item("may", "issue", 15)["status"] == "working"

    fresh_db.upsert_item("may", "issue", 16, "A bug", "a", "open", "x")
    fresh_db.update_item("may", "issue", 16, status="waiting_human",
                         gh_state="closed")
    q = _asked(fresh_db, "issue#16")
    fresh_db.answer_question(q["id"], "Fix")
    assert pipeline.route_answers(may) == []
