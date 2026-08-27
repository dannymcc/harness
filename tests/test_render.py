"""Rendering the app, so UI work is not done blind.

Two halves: harness/render.py itself (what it reports about a page, and that
it starts and stops the app around the browser), and the wiring that puts it
in front of an engineer — the per-project preview command, the screenshot
directory that must never reach a commit, and the prompt that names both.

The browser is not exercised here: it is in the image, not in every checkout,
and what is worth pinning is the verdict the script draws from a page, which
is ordinary Python.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from harness import render
from harness.gh import run


def git(cwd, *args, **kw):
    return run(["git", *args], cwd=cwd, **kw)


# --- what the script makes of a page -----------------------------------------

def test_viewports_parse_or_say_why_not():
    assert render.parse_viewports(["412x915", " 1280 X 800 "]) == [
        (412, 915), (1280, 800)]
    assert render.parse_viewports(None) == [(412, 915), (1280, 800)]
    with pytest.raises(ValueError):
        render.parse_viewports(["phone"])


def test_screenshot_names_say_which_page_at_which_width():
    assert render.shot_name("/", 412, 915) == "root-412x915.png"
    assert render.shot_name("/projects/1/edit", 1280, 800) == \
        "projects-1-edit-1280x800.png"


def test_a_page_that_fits_is_reported_clean():
    page = {"route": "/", "viewport": "412x915", "status": 200,
            "scrollWidth": 412, "clientWidth": 412, "overflowing": [],
            "overflowingCount": 0, "consoleErrors": []}
    assert render.page_findings(page) == []
    assert render.exit_code({"pages": [page]}) == 0
    assert "ok   / @ 412x915" in render.summarise({"pages": [page]})


def test_the_two_faults_a_stylesheet_reads_clean_on_are_reported():
    """A page wider than the phone, and the element making it so."""
    page = {"route": "/", "viewport": "412x915", "status": 200,
            "scrollWidth": 980, "clientWidth": 412,
            "overflowing": [{"selector": "input#search", "right": 640,
                             "width": 300, "text": "Search"}],
            "overflowingCount": 3, "consoleErrors": ["TypeError: x is null"]}
    findings = render.page_findings(page)
    assert any("scrolls sideways" in f and "980" in f for f in findings)
    assert any("input#search" in f for f in findings)
    assert any("2 more" in f for f in findings)
    assert any("console error" in f for f in findings)
    # Rendered-with-findings is a verdict, not a crash: the PNGs are there.
    assert render.exit_code({"pages": [page]}) == 2
    assert "FAIL / @ 412x915" in render.summarise({"pages": [page]})


def test_sub_pixel_rounding_is_not_a_finding():
    page = {"route": "/", "viewport": "412x915", "status": 200,
            "scrollWidth": 413, "clientWidth": 412, "overflowing": [],
            "overflowingCount": 0, "consoleErrors": []}
    assert render.page_findings(page) == []


def test_a_page_that_did_not_render_says_so_and_nothing_else():
    page = {"route": "/x", "viewport": "412x915", "error": "TimeoutError: 30s",
            "scrollWidth": 0, "clientWidth": 0, "consoleErrors": ["noise"]}
    assert render.page_findings(page) == ["did not render: TimeoutError: 30s"]


def test_no_pages_at_all_is_a_failed_run():
    assert render.exit_code({"pages": []}) == 1


# --- starting and stopping the app -------------------------------------------

def test_the_app_never_sees_harness_credentials(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_secret_token")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_token")
    env = render.child_env("/venv/bin")
    assert env["GH_TOKEN"] == "" and env["GITHUB_TOKEN"] == ""
    assert env["PATH"].startswith("/venv/bin:")


def test_a_preview_command_that_dies_is_reported_not_waited_out():
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"],
                            start_new_session=True)
    why = render.wait_for_app("http://127.0.0.1:1/", proc, timeout=30)
    assert "exited with status 3" in why


def test_stop_app_takes_the_whole_process_group_down():
    proc = subprocess.Popen(["bash", "-c", "sleep 60 & wait"],
                            start_new_session=True)
    render.stop_app(proc)
    assert proc.poll() is not None


def test_main_reports_a_dead_app_and_writes_the_report(tmp_path):
    out = tmp_path / "shots"
    code = render.main(["--command", "echo nothing here; exit 1",
                        "--base-url", "http://127.0.0.1:1/",
                        "--out", str(out), "--start-timeout", "20"])
    assert code == 1
    import json
    report = json.loads((out / "report.json").read_text())
    assert "exited with status 1" in report["error"]
    assert report["pages"] == []
    # The app's own output is kept: "it did not start" is useless without it.
    assert "nothing here" in (out / "app.log").read_text()


# --- the preview command an engineer is given --------------------------------

def test_no_preview_command_means_no_renderer_and_no_instructions(may):
    from harness import agents, repo
    assert repo.render_command(may) == ""
    assert agents._render_note(may) == ""


def test_a_preview_command_names_the_script_the_app_and_the_venv(fresh_db):
    from harness import agents, repo
    fresh_db.create_project("june", "example/june",
                            preview_command="DEMO=true python app.py")
    p = fresh_db.get_project("june")
    cmd = repo.render_command(p)
    assert str(repo.RENDER_SCRIPT) in cmd
    assert "'DEMO=true python app.py'" in cmd     # one argument, quoted
    assert str(repo.venv_dir(p) / "bin") in cmd
    assert repo.SCREENSHOT_DIR in cmd
    note = agents._render_note(p)
    assert cmd in note and "412x915" in note


def test_the_engineer_is_told_to_render_only_where_there_is_a_ui(
        fresh_db, monkeypatch):
    """The fix prompt carries the invocation; a project with no UI is not
    asked for screenshots that cannot exist."""
    import asyncio
    from harness import agents
    from test_agent_safety import _capture

    seen = _capture(monkeypatch)
    fresh_db.create_project("june", "example/june",
                            preview_command="python app.py")
    p = dict(fresh_db.get_project("june"))
    issue = {"number": 7, "title": "t", "body": "b"}
    asyncio.run(agents.fix_issue(p, issue, "the plan", "/tmp"))
    assert "render.py" in seen["prompt"]
    assert "not optional" in seen["prompt"]

    p["preview_command"] = ""
    asyncio.run(agents.fix_issue(p, issue, "the plan", "/tmp"))
    assert "render.py" not in seen["prompt"]


def test_the_reviewing_roles_are_not_given_a_browser(fresh_db, monkeypatch):
    """A browser is a general-purpose network client, and the containment of
    the roles that read text from the internet is that they cannot run one.
    They read the engineer's PNGs with Read instead."""
    import asyncio
    from harness import agents
    from test_agent_safety import _capture

    seen = _capture(monkeypatch)
    fresh_db.create_project("june", "example/june",
                            preview_command="python app.py")
    p = dict(fresh_db.get_project("june"))
    asyncio.run(agents.triage_issue(p, {"number": 7, "title": "t",
                                        "body": "b"}, "/tmp"))
    assert "render.py" not in seen["prompt"]
    assert seen["readonly"] is True
    asyncio.run(agents.review_pr(p, {"number": 8, "author": {"login": "a"}},
                                 "diff",
                                 "tests", "/tmp"))
    assert "render.py" not in seen["prompt"]
    assert not any("render" in rule for rule in seen["bash_rules"])


# --- screenshots are evidence, not commits -----------------------------------

@pytest.fixture()
def checkout(fresh_db, tmp_path):
    """A git repo standing in for the engineer's worktree."""
    d = tmp_path / "wt"
    d.mkdir()
    git(d, "init", "-q", "-b", "dev")
    git(d, "config", "user.email", "harness@example.com")
    git(d, "config", "user.name", "Harness")
    (d / "app.py").write_text("x = 1\n")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "initial")
    git(d, "update-ref", "refs/remotes/origin/dev", "HEAD")
    fresh_db.create_project("may", "example/may",
                            preview_command="python app.py")
    return dict(fresh_db.get_project("may")), d


def test_the_screenshot_directory_is_made_and_hidden_from_git(checkout):
    from harness import repo
    project, d = checkout
    shots = repo.ensure_screenshot_dir(project, d)
    assert shots == d / repo.SCREENSHOT_DIR and shots.is_dir()
    (shots / "root-412x915.png").write_bytes(b"\x89PNG")
    # Otherwise a run that only rendered reads as a run that changed the code.
    assert git(d, "status", "--porcelain").strip() == ""
    assert repo.wt_has_changes(project, d) is False
    # Twice is once: the exclude is not appended on every dispatch.
    repo.ensure_screenshot_dir(project, d)
    exclude = (d / ".git" / "info" / "exclude").read_text()
    assert exclude.count(".harness/") == 1


def test_screenshots_never_reach_the_commit(checkout):
    """Belt (the exclude) and braces (the commit itself): a directory of PNGs
    pushed to dev would be a fix nobody asked for."""
    from harness import repo
    project, d = checkout
    shots = d / repo.SCREENSHOT_DIR
    shots.mkdir(parents=True)
    (shots / "root-412x915.png").write_bytes(b"\x89PNG")
    (d / "app.py").write_text("x = 2\n")
    repo.wt_commit_all(project, d, "fix: something")
    assert git(d, "show", "--name-only", "--format=", "HEAD").split() == \
        ["app.py"]
    assert (shots / "root-412x915.png").exists()   # still there to look at


def test_a_run_that_only_rendered_still_counts_as_no_change(checkout):
    """Even with the exclude missing, PNGs must not make an engineer who
    changed nothing look like one who did."""
    from harness import repo
    project, d = checkout
    shots = d / repo.SCREENSHOT_DIR
    shots.mkdir(parents=True)
    (shots / "root-412x915.png").write_bytes(b"\x89PNG")
    assert repo.wt_has_changes(project, d) is False
    (d / "app.py").write_text("x = 3\n")
    assert repo.wt_has_changes(project, d) is True


def test_the_thread_says_where_the_screenshots_are(checkout, fresh_db):
    """Excluded from the commit means the diff never mentions them; the
    thread has to, or the evidence is there and nobody knows."""
    from harness import pipeline, repo
    project, d = checkout
    fresh_db.upsert_item("may", "issue", 1, "t", "a", "open", "x")
    pipeline._note_screenshots("may", "issue#1", d)     # nothing rendered yet
    (d / repo.SCREENSHOT_DIR).mkdir(parents=True)
    (d / repo.SCREENSHOT_DIR / "root-412x915.png").write_bytes(b"\x89PNG")
    pipeline._note_screenshots("may", "issue#1", d)
    thread = fresh_db.thread("may", "issue#1")
    assert len(thread) == 1
    assert "root-412x915.png" in thread[0]["text"]


def test_a_linked_worktree_shares_the_clone_exclude(checkout, tmp_path):
    """Worktrees read excludes from the common .git, not their own gitdir."""
    from harness import repo
    project, d = checkout
    wt = tmp_path / "linked"
    git(d, "worktree", "add", "-q", "-b", "harness/issue-1", str(wt))
    assert repo.ensure_screenshot_dir(project, wt) == wt / repo.SCREENSHOT_DIR
    (wt / repo.SCREENSHOT_DIR / "root-412x915.png").write_bytes(b"\x89PNG")
    assert git(wt, "status", "--porcelain").strip() == ""


def test_an_unwritable_clone_says_so_instead_of_failing_the_fix(
        checkout, monkeypatch, fresh_db):
    """The exclude is not the guarantee — the commit is — so a clone that
    will not take one costs an event, not the run."""
    from harness import repo
    project, d = checkout

    def boom(cmd, cwd=None, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(repo, "run", boom)
    assert repo.ensure_screenshot_dir(project, d) is None
    assert not (d / repo.SCREENSHOT_DIR).exists()
    assert any("from git status" in e["message"]
               for e in fresh_db.recent_events(project="may"))


# --- the operator's switch ---------------------------------------------------

def test_the_preview_command_is_set_on_the_add_form(client, fresh_db):
    r = client.post("/add", data={"name": "June", "gh_repo": "example/june",
                                  "preview_command": "python app.py"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert fresh_db.get_project("june")["preview_command"] == "python app.py"


def test_the_preview_command_is_editable_afterwards(client, fresh_db):
    """A project grows a UI long after it is added."""
    r = client.post("/p/may/preview-command", data={"value": " python app.py "},
                    follow_redirects=False)
    assert r.status_code == 303
    assert fresh_db.get_project("may")["preview_command"] == "python app.py"
    assert "python app.py" in client.get("/p/may/settings").text
    client.post("/p/may/preview-command", data={"value": ""})
    assert fresh_db.get_project("may")["preview_command"] == ""


def test_an_old_database_gains_the_column(fresh_db):
    """Old data/harness.db files have to keep loading."""
    assert any("projects ADD COLUMN preview_command" in m
               for m in fresh_db.MIGRATIONS)
    with fresh_db.conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(projects)")}
    assert "preview_command" in cols
