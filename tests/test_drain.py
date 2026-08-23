"""SIGTERM drains: in-flight runs finish, nothing new starts, the loop exits."""
import asyncio
import threading
import time

import pytest


@pytest.fixture()
def drain_state():
    from harness import worker
    saved = dict(worker._state)
    worker._state.update({"draining": False, "thread": None, "running": False})
    yield worker
    worker._state.clear()
    worker._state.update(saved)
    worker._state["draining"] = False


def test_run_agent_refuses_to_start_while_draining(fresh_db, may, drain_state,
                                                   monkeypatch):
    from harness import agents
    worker = drain_state
    # the SDK must never be reached: make it explode if it is
    monkeypatch.setattr(agents, "ClaudeSDKClient",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("SDK started")))
    worker.request_drain(on_done=None)
    with pytest.raises(agents.AgentStalled, match="draining"):
        asyncio.run(agents.run_agent(project_name="may", role="ic", item_key="issue#1",
                                     task="fix", prompt="x", cwd=None, schema={}))
    assert fresh_db.consecutive_failures("may", "issue#1") == 0  # nothing recorded


def test_a_draining_cycle_parks_the_item_without_failing_it(fresh_db, may,
                                                             drain_state, monkeypatch):
    """A fix that has not started when the drain begins is left approved
    (retry after restart), not marked failed; no breaker count."""
    from harness import pipeline, agents
    worker = drain_state
    fresh_db.upsert_item("may", "issue", 9, "t", "a", "open", "x")
    fresh_db.update_item("may", "issue", 9, status="approved")

    async def fake_fix(project, item, persona):
        raise agents.AgentStalled("draining for restart")
    from harness import repo
    monkeypatch.setattr(pipeline, "fix_item", fake_fix)
    monkeypatch.setattr(pipeline, "sync", lambda p: None)
    monkeypatch.setattr(pipeline, "_reconcile_branches", lambda p: None)
    monkeypatch.setattr(pipeline, "_release_due", lambda p: None)
    monkeypatch.setattr(pipeline, "_budget_hold", lambda p: False)
    monkeypatch.setattr(repo, "clean_checkout", lambda p, b: "/tmp")
    monkeypatch.setattr(repo, "ensure_test_env", lambda p: None)
    worker.request_drain(on_done=None)
    asyncio.run(pipeline.run_cycle(may, force=True))
    item = fresh_db.get_item("may", "issue", 9)
    assert item["status"] in ("approved", "working")
    assert fresh_db.consecutive_failures("may", "issue#9") == 0


def test_worker_loop_exits_once_drained(fresh_db, drain_state, monkeypatch):
    from harness import worker, pipeline, housekeeping
    ran = []

    async def _noop(*a, **k):
        ran.append(1)
        return False
    monkeypatch.setattr(pipeline, "process_directives", _noop)
    monkeypatch.setattr(pipeline, "process_questions", _noop)
    monkeypatch.setattr(pipeline, "run_all_cycles", _noop)
    monkeypatch.setattr(pipeline, "standup_due", lambda: False)
    monkeypatch.setattr(housekeeping, "due", lambda: False)
    monkeypatch.setattr(worker.config, "POLL_INTERVAL_MINUTES", 60)

    t = threading.Thread(target=worker._loop, daemon=True)
    worker._state["thread"] = t
    t.start()
    deadline = time.time() + 5
    while not ran and time.time() < deadline:
        time.sleep(0.05)
    assert ran, "loop never ran a cycle"
    assert t.is_alive()                       # idle, waiting an hour

    done = threading.Event()
    worker.request_drain(on_done=done.set, timeout_s=5)
    assert done.wait(5)
    assert not t.is_alive()                   # woke and left instead of waiting
    assert worker.status()["draining"] is True
    msgs = [r["message"] for r in fresh_db.recent_events(20)]
    assert any("Draining for restart" in m for m in msgs)
    assert any("Drained" in m for m in msgs)


def test_second_sigterm_exits_at_once(drain_state, monkeypatch):
    import signal
    import run as entry
    srv = entry._Server(entry.uvicorn.Config(entry.app))
    calls = []
    monkeypatch.setattr(drain_state, "request_drain",
                        lambda on_done=None, timeout_s=None: calls.append("drain"))
    srv.handle_exit(signal.SIGTERM, None)
    assert calls == ["drain"] and srv.should_exit is False
    drain_state._state["draining"] = True
    srv.handle_exit(signal.SIGTERM, None)
    assert srv.should_exit is True
