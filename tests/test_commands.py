"""Slash commands in the composer.

The same box takes both: a leading `/` is carried out here and now against
the routes the buttons use, anything else is still a direction for Harry.
"""


def test_approve_resolves_the_number_against_the_desk(client, fresh_db):
    fresh_db.upsert_item("may", "issue", 4, "A bug", "alice", "open", "x")
    r = client.post("/p/may/tell", data={"text": "/approve 4"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert fresh_db.get_item("may", "issue", 4)["status"] == "approved"
    # no direction was filed: a command is not something Harry judges
    assert not fresh_db.pending_directives("may")
    # and the stream shows what was typed as well as what it did
    messages = [e["message"] for e in fresh_db.recent_events(10, "may")]
    assert any(m.endswith("/approve 4") for m in messages)


def test_number_may_be_a_pr(client, fresh_db):
    """GitHub numbers issues and PRs from one sequence, so /approve looks."""
    fresh_db.upsert_item("may", "pr", 6, "A patch", "bob", "open", "x")
    client.post("/p/may/tell", data={"text": "/reject 6"})
    assert fresh_db.get_item("may", "pr", 6)["status"] == "rejected"

    r = client.post("/p/may/tell", data={"text": "/approve 99"})
    assert r.status_code == 400 and "no issue or pr #99" in r.text


def test_merge_is_approve_on_an_unreviewed_pr(client, fresh_db):
    """/merge pr N is an alias, not a new outward action: the approve route
    already sends an unreviewed PR to a tested merge."""
    fresh_db.upsert_item("may", "pr", 8, "A patch", "bob", "open", "x")
    client.post("/p/may/tell", data={"text": "/merge pr 8"})
    assert fresh_db.get_item("may", "pr", 8)["status"] == "approved"
    assert any("straight to merge" in e["message"]
               for e in fresh_db.recent_events(10, "may"))


def test_budget_and_policy_from_the_overview(client, fresh_db):
    """On / the desk is named, or taken from the composer's own select."""
    r = client.post("/tell", data={"project": "may", "text": "/budget may 100"})
    assert r.status_code == 200  # followed the redirect to the settings page
    assert fresh_db.policy("may", "daily_budget_usd") == "100"

    client.post("/tell", data={"project": "may", "text": "/budget 55"})
    assert fresh_db.policy("may", "daily_budget_usd") == "55"

    client.post("/tell", data={"project": "may",
                               "text": "/policy may fix_issues approve"})
    assert fresh_db.policy("may", "fix_issues") == "approve"


def test_bad_policy_and_budget_are_refused_not_stored(client, fresh_db):
    """set_policy ignores an unknown key silently — say so instead."""
    r = client.post("/tell", data={"project": "may",
                                   "text": "/policy may fix_isues auto"})
    assert r.status_code == 400 and "No policy called" in r.text

    r = client.post("/tell", data={"project": "may", "text": "/budget lots"})
    assert r.status_code == 400 and "takes a number" in r.text
    assert fresh_db.policy("may", "daily_budget_usd") == "30"  # the default

    r = client.post("/tell", data={"project": "may",
                                   "text": "/policy may cut_release maybe"})
    assert r.status_code == 400 and "auto or approve" in r.text

    r = client.post("/tell", data={"project": "may", "text": "/budget june 10"})
    assert r.status_code == 400 and "No desk called 'june'" in r.text


def test_tell_steers_the_named_agent(client, fresh_db):
    fresh_db.upsert_item("may", "issue", 11, "Footer", "alice", "open", "x")
    rid = fresh_db.start_run("may", "ic", "issue#11", "fix", "m", "Malcolm")

    r = client.post("/p/may/tell", data={"text": "/tell Malcolm skip the probe"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/run/{rid}"
    assert [s["text"] for s in fresh_db.run_steers(rid)] == ["skip the probe"]

    # the case the operator will actually hit: nobody is running
    fresh_db.finish_run(rid, True, 0.1, 1, "done")
    r = client.post("/p/may/tell", data={"text": "/tell Malcolm skip the probe"})
    assert r.status_code == 400
    assert "no live run" in r.text and "direction" in r.text
    assert len(fresh_db.run_steers(rid)) == 1


def test_tell_asks_which_run_when_two_are_live(client, fresh_db):
    a = fresh_db.start_run("may", "ic", "issue#1", "fix", "m", "Malcolm")
    b = fresh_db.start_run("may", "ic", "issue#2", "fix", "m", "Malcolm")
    r = client.post("/p/may/tell", data={"text": "/tell Malcolm wait"})
    assert r.status_code == 400 and str(a) in r.text and str(b) in r.text

    client.post("/p/may/tell", data={"text": f"/tell {b} wait"})
    assert [s["text"] for s in fresh_db.run_steers(b)] == ["wait"]


def test_tell_falls_back_to_the_persona_of_an_unnamed_run(client, fresh_db):
    """Older run rows have no agent recorded; the role and task still say."""
    rid = fresh_db.start_run("may", "ic", "issue#3", "triage", "m")
    client.post("/p/may/tell", data={"text": "/tell ruth look at the tests"})
    assert [s["text"] for s in fresh_db.run_steers(rid)] == ["look at the tests"]


def test_stop_asks_for_a_live_run(client, fresh_db):
    rid = fresh_db.start_run("may", "ic", "issue#5", "fix", "m", "Malcolm")
    client.post("/p/may/tell", data={"text": f"/stop {rid}"})
    assert fresh_db.cancel_requested(rid)

    fresh_db.finish_run(rid, True, 0.1, 1, "done")
    r = client.post("/p/may/tell", data={"text": f"/stop {rid}"})
    assert r.status_code == 400 and "not running" in r.text


def test_release_says_why_it_cannot(client, fresh_db, monkeypatch):
    from harness import pipeline
    monkeypatch.setattr(pipeline, "anything_to_release", lambda p, queued=None: False)
    r = client.post("/p/may/tell", data={"text": "/release"})
    assert r.status_code == 400 and "Nothing to release" in r.text
    assert fresh_db.get_setting("release_requested.may") != "1"

    monkeypatch.setattr(pipeline, "anything_to_release", lambda p, queued=None: True)
    client.post("/p/may/tell", data={"text": "/release may"})
    assert fresh_db.get_setting("release_requested.may") == "1"


def test_jump_and_cycle(client, fresh_db):
    r = client.post("/tell", data={"project": "may", "text": "/p may"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/p/may"

    r = client.post("/tell", data={"project": "may", "text": "/p june"})
    assert r.status_code == 400 and "No desk called 'june'" in r.text

    assert client.post("/p/may/tell", data={"text": "/cycle"}).status_code == 200


def test_unknown_command_returns_the_cheatsheet(client, fresh_db):
    r = client.post("/p/may/tell", data={"text": "/bogus 4"})
    assert r.status_code == 400
    assert "No such command: /bogus" in r.text and "/approve" in r.text
    assert not fresh_db.pending_directives("may")

    r = client.post("/p/may/tell", data={"text": "/?"})
    assert r.status_code == 200 and "/approve" in r.text
    assert not fresh_db.pending_directives("may")


def test_prose_still_goes_to_harry(client, fresh_db):
    """Including prose with a slash in it — only a leading / is a command."""
    for text in ("Focus on the flaky test",
                 "look at the and/or handling in db.py"):
        client.post("/p/may/tell", data={"text": text})
    assert [d["question"] for d in fresh_db.pending_directives("may")] == [
        "Focus on the flaky test", "look at the and/or handling in db.py"]


def test_cheatsheet_is_on_the_page(client, fresh_db):
    fresh_db.upsert_item("may", "issue", 2, "A bug", "alice", "open", "x")
    for path in ("/", "/p/may", "/p/may/issue/2"):
        assert 'class="cheatsheet"' in client.get(path).text, path
