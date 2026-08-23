def test_pages_render(client, fresh_db):
    fresh_db.upsert_item("may", "issue", 7, "A bug", "alice", "open", "x")
    for path in ("/", "/p/may", "/p/may/settings", "/p/may/issue/7", "/add",
                 "/static/manifest.json", "/static/icon.svg"):
        assert client.get(path).status_code == 200, path


def test_version_in_footer(client):
    from harness import config
    html = client.get("/").text
    assert config.DISPLAY_VERSION in html


def test_question_buttons_and_ntfy_answer(client, fresh_db):
    fresh_db.ask_question("may", "Ruth", "", "Pick one", options=["A", "B"])
    q = fresh_db.open_questions("may")[0]
    assert "option-form" in client.get("/p/may").text
    r = client.post(f"/p/may/question/{q['id']}/answer?via=ntfy",
                    data={"answer": "A"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_health_reports_worker_down(client):
    assert client.get("/health").status_code == 503  # no worker in tests


def test_board_shows_live_assignee(client, fresh_db):
    fresh_db.upsert_item("may", "issue", 12, "Busy item", "bob", "open", "x")
    fresh_db.update_item("may", "issue", 12, status="working")
    fresh_db.start_run("may", "ic", "issue#12", "fix", "m", "Dimitri")  # live
    html = client.get("/p/may").text
    assert "Dimitri · working" in html and "assignee live" in html
    assert "1 live" in html


def test_staff_chip_states_and_links(client, fresh_db):
    from harness.web.app import _member_status
    rid = fresh_db.start_run("may", "ic", "issue#1", "fix", "m", "Malcolm")
    fresh_db.finish_run(rid, False, 0.1, 1, "orphaned by restart")
    runs = fresh_db.recent_runs(10, "may")
    m = _member_status("Malcolm", runs, lambda r: r["agent"] == "Malcolm")
    assert m["state"] == "restarted" and "requeued" in m["detail"]
    rid2 = fresh_db.start_run("may", "ic", "issue#2", "fix", "m", "Beth")
    fresh_db.finish_run(rid2, False, 0.1, 1, "tests failed after fix")
    runs = fresh_db.recent_runs(10, "may")
    b = _member_status("Beth", runs, lambda r: r["agent"] == "Beth")
    assert b["state"] == "failed" and "tests failed" in b["detail"]
    assert f"/run/{rid}" in client.get("/").text  # Malcolm is on-roster


def test_directions_visible_after_tell(client, fresh_db):
    r = client.post("/p/may/tell", data={"text": "Focus on bugs this week"},
                    follow_redirects=False)
    assert r.status_code == 303
    html = client.get("/p/may").text
    assert "Focus on bugs this week" in html
    assert "directions-list" in html


def test_theme_defaults_to_light_and_persists(client):
    """No cookie means light, whatever the operating system prefers."""
    html = client.get("/").text
    assert 'data-theme="light"' in html
    assert '<meta name="theme-color" content="#fffdf8">' in html
    assert 'action="/theme"' in html

    r = client.post("/theme", data={"value": "dark"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.cookies.get("theme") == "dark"
    html = client.get("/").text
    assert 'data-theme="dark"' in html
    assert '<meta name="theme-color" content="#1D5741">' in html

    client.cookies.set("theme", "chartreuse")  # junk falls back to light
    assert 'data-theme="light"' in client.get("/").text


def test_theme_post_ignores_a_foreign_referer(client):
    r = client.post("/theme", data={"value": "dark"},
                    headers={"referer": "https://example.com/evil"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_run_tail_streams_increments(client, fresh_db, tmp_path):
    rid = fresh_db.start_run("may", "ic", "issue#3", "fix", "m", "Malcolm")
    log = tmp_path / "run.log"
    log.write_text("hello ")
    with fresh_db.conn() as c:
        c.execute("UPDATE runs SET log_path = ? WHERE id = ?", (str(log), rid))
    j = client.get(f"/run/{rid}/tail?offset=0").json()
    assert j["data"] == "hello " and j["live"] is True
    log.write_text("hello world")
    j2 = client.get(f"/run/{rid}/tail?offset={j['offset']}").json()
    assert j2["data"] == "world"
    fresh_db.finish_run(rid, True, 0.1, 1, "done")
    j3 = client.get(f"/run/{rid}/tail?offset={j2['offset']}").json()
    assert j3["live"] is False


def test_overview_composer_files_direction(client, fresh_db):
    r = client.post("/tell", data={"project": "may",
                                   "text": "Add CSV export to reports"},
                    follow_redirects=False)
    assert r.status_code == 303
    pend = fresh_db.pending_directives("may")
    assert pend and pend[0]["question"] == "Add CSV export to reports"
    assert "Send to Harry" in client.get("/").text


def test_only_escalations_get_primary_buttons(client, fresh_db):
    fresh_db.ask_question("may", "Ruth", "", "Harrys call", options=["A", "B"])
    fresh_db.ask_question("may", "Adam", "", "Dannys call", options=["X", "Y"])
    adam = [q for q in fresh_db.harry_inbox("may") if q["asked_by"] == "Adam"][0]
    fresh_db.escalate_question(adam["id"])
    html = client.get("/p/may").text
    assert "Needs your decision (1)" in html and "With Harry (1)" in html
    # the operator's own question is answerable at a tap; Harry's is tucked away
    assert html.index("Dannys call") < html.index("Harrys call")
    assert "Answer it yourself instead" in html
    html = client.get("/").text
    assert "Needs your decision (1)" in html


def test_release_now_button_and_request(client, fresh_db):
    html = client.get("/p/may").text
    assert "Release now (Colin)" in html
    r = client.post("/p/may/release/request", follow_redirects=False)
    assert r.status_code == 303
    assert fresh_db.get_setting("release_requested.may") == "1"
    # the button gives way to the pending state, so it cannot be pressed twice
    html = client.get("/p/may").text
    assert "Release requested" in html and "Release now (Colin)" not in html


def test_release_now_is_refused_while_one_is_open(client, fresh_db):
    fresh_db.create_release("may", "1.0.0", "notes", [])
    client.post("/p/may/release/request", follow_redirects=False)
    assert fresh_db.get_setting("release_requested.may") == ""
    assert "Release now (Colin)" not in client.get("/p/may").text


def test_auto_release_is_visible_on_the_project(client, fresh_db):
    assert "pill ok\">auto" not in client.get("/p/may").text
    fresh_db.set_policy("may", "cut_release", "auto")
    html = client.get("/p/may").text
    assert "pill ok\">auto" in html
    assert "merges and tags itself" in html


def test_auto_release_shows_on_overview_and_is_not_your_queue(client, fresh_db):
    fresh_db.create_release("may", "1.0.0", "notes", [])
    assert "release v1.0.0 proposed" in client.get("/").text
    fresh_db.set_policy("may", "cut_release", "auto")
    html = client.get("/").text
    assert "auto release" in html and "release v1.0.0 going out" in html


def test_live_console_keeps_polling_before_the_log_exists(client, fresh_db):
    """A live run with nothing written yet must not report live=False — the
    poller stops for good on that, which reads as a stalled agent."""
    rid = fresh_db.start_run("may", "ic", "issue#4", "fix", "m", "Dimitri")
    j = client.get(f"/run/{rid}/tail?offset=0").json()
    assert j["data"] == "" and j["live"] is True
    fresh_db.finish_run(rid, True, 0.1, 3, "done")
    assert client.get(f"/run/{rid}/tail?offset=0").json()["live"] is False


def test_overview_release_button_overrides_the_thresholds(client, fresh_db):
    """One queued change is under the default batch of three; the override
    exists precisely so that does not mean waiting."""
    fresh_db.upsert_item("may", "issue", 1, "t", "a", "open", "x")
    fresh_db.update_item("may", "issue", 1, status="queued",
                         queued_at=fresh_db.now())
    assert "Release now" in client.get("/").text
    client.post("/p/may/release/request", follow_redirects=False)
    html = client.get("/").text
    assert "release requested" in html and "Release now" not in html

    from harness import pipeline
    # one queued item, threshold three: only the request gets it over the line
    assert len(pipeline._release_due(fresh_db.get_project("may"))) == 1
