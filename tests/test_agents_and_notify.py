import http.server
import json
import threading


def test_stall_detection(fresh_db):
    from harness.agents import _check_stall, _stall_reset_time
    assert _check_stall("Error 429: rate limit exceeded")
    assert _check_stall("usage limit reached|1893456000")
    assert not _check_stall("AssertionError: tests failed")
    assert _stall_reset_time("resets at 1893456000") == "2030-01-01T00:00:00Z"


def test_notify_payload_and_resilience(fresh_db):
    from harness import config, notify
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.update(json.loads(
                self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    config.NTFY_URL = f"http://127.0.0.1:{srv.server_port}"
    config.NTFY_TOPIC = "t"
    config.PUBLIC_URL = "http://example:8300"
    notify.send("Title", "Body", priority="high", tags="question",
                click_path="/p/may",
                actions=[{"label": "Yes", "path": "/x?via=ntfy", "body": "answer=Yes"}])
    assert seen["priority"] == 4
    assert seen["actions"][0]["url"].endswith("/x?via=ntfy")
    config.NTFY_URL = "http://127.0.0.1:1"
    notify.send("x", "y")  # unreachable server must not raise
    config.NTFY_TOPIC = ""


def test_run_progress_is_visible_while_the_run_is_in_flight(fresh_db):
    """log_path and turns are what the GUI reads to show a run working."""
    rid = fresh_db.start_run("may", "ic", "issue#5", "fix", "m", "Malcolm")
    assert fresh_db.get_run(rid)["log_path"] == ""
    fresh_db.update_run(rid, log_path="/tmp/run.log", turns=2)
    run = fresh_db.get_run(rid)
    assert run["log_path"] == "/tmp/run.log" and run["turns"] == 2
    assert run["finished_at"] is None  # progress never ends a run
