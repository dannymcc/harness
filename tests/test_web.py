import pytest


@pytest.fixture()
def client(fresh_db, may):
    from fastapi.testclient import TestClient
    from harness.web.app import app
    return TestClient(app)


def test_pages_render(client, fresh_db):
    fresh_db.upsert_item("may", "issue", 7, "A bug", "alice", "open", "x")
    for path in ("/", "/p/may", "/p/may/settings", "/p/may/issue/7", "/add",
                 "/static/manifest.json", "/static/icon.svg"):
        assert client.get(path).status_code == 200, path


def test_version_in_footer(client):
    from harness import config
    assert f"harness v{config.VERSION}" in client.get("/").text


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
