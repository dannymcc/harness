"""Every desk has its own wake loop.

An action on an idle desk — approving a release, answering a question — is
served on the click, rather than queueing behind whatever the slowest desk
happens to be in the middle of.
"""
import asyncio
import threading
import time

import pytest


def _wait(predicate, timeout=8.0):
    """Poll until the predicate holds; return whether it did."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def worker_loop(fresh_db, monkeypatch):
    """Start the real worker thread with the section's shared chores stubbed.

    Yields a callable that starts the thread; the teardown drains it, so no
    loop outlives the test's database."""
    from harness import worker, pipeline, housekeeping

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(pipeline, "process_directives", _noop)
    monkeypatch.setattr(pipeline, "process_questions", _noop)
    monkeypatch.setattr(pipeline, "standup_due", lambda: False)
    monkeypatch.setattr(pipeline, "work_ready", lambda p: False)
    monkeypatch.setattr(housekeeping, "due", lambda: False)
    monkeypatch.setattr(worker.config, "POLL_INTERVAL_MINUTES", 60)
    saved = dict(worker._state)
    worker._state.update({"draining": False, "thread": None, "loop": None,
                          "chores": None, "desks": {}, "busy": set()})
    threads = []

    def _start():
        t = threading.Thread(target=worker._loop, name="test-worker",
                             daemon=True)
        worker._state["thread"] = t
        threads.append(t)
        t.start()
        return t

    yield _start

    worker._state["draining"] = True
    worker._wake()
    for t in threads:
        t.join(10)
    alive = [t for t in threads if t.is_alive()]
    worker._state.clear()
    worker._state.update(saved)
    worker._state["draining"] = False
    assert not alive, "the worker thread outlived the test"


def test_an_action_on_an_idle_desk_does_not_wait_for_another_desks_wave(
        fresh_db, worker_loop, monkeypatch):
    """The bug: roan's release sat queued behind may's fix wave, because one
    global wake only came round when the slowest desk's cycle ended."""
    from harness import pipeline, worker
    for n in ("alpha", "beta"):
        fresh_db.create_project(n, f"example/{n}")
    hold = threading.Event()
    done = []          # (desk, when it finished), in order

    async def fake_cycle(project, force=False):
        name = project["name"]
        while name == "alpha" and not hold.is_set():
            await asyncio.sleep(0.01)   # alpha is deep in a long wave
        done.append((name, time.monotonic()))
    monkeypatch.setattr(pipeline, "run_cycle", fake_cycle)

    worker_loop()
    assert _wait(lambda: any(d[0] == "beta" for d in done))
    assert not any(d[0] == "alpha" for d in done)   # alpha still in flight

    clicked = time.monotonic()
    worker.trigger("beta")
    assert _wait(lambda: len([d for d in done if d[0] == "beta"]) == 2), \
        "beta's cycle never ran while alpha's was in flight"
    served = [d for d in done if d[0] == "beta"][1][1]
    assert not any(d[0] == "alpha" for d in done), \
        "beta was only served once alpha's wave had finished"
    assert served - clicked < 3

    hold.set()
    assert _wait(lambda: any(d[0] == "alpha" for d in done))


def test_two_triggers_on_one_desk_do_not_overlap_its_cycles(
        fresh_db, may, worker_loop, monkeypatch):
    """A desk's loop is its lock: whatever arrives mid-cycle is served on the
    next pass, never as a second wave in the same worktree pool."""
    from harness import pipeline, worker
    spans = []
    forced = []

    async def fake_cycle(project, force=False):
        t0 = time.monotonic()
        forced.append(force)
        await asyncio.sleep(0.2)
        spans.append((t0, time.monotonic()))
    monkeypatch.setattr(pipeline, "run_cycle", fake_cycle)

    worker_loop()
    assert _wait(lambda: spans)          # the desk's first pass, now idle
    worker.trigger("may")
    assert _wait(lambda: len(forced) == 2)
    worker.trigger("may")               # ...while that cycle is still running
    assert _wait(lambda: len(spans) >= 2)
    time.sleep(0.5)                     # room for an overlapping run to show up

    for earlier, later in zip(spans, spans[1:]):
        assert earlier[1] <= later[0], f"cycles overlapped: {spans}"
    assert forced[1] is True            # a click runs even off the clock


def test_desks_run_their_cycles_concurrently(fresh_db, worker_loop, monkeypatch):
    """A slow desk must not delay the others — no desk waits on a gather."""
    from harness import pipeline
    for n in ("alpha", "beta", "gamma"):
        fresh_db.create_project(n, f"example/{n}")
    spans = {}

    async def fake_cycle(project, force=False):
        t0 = time.monotonic()
        await asyncio.sleep(0.3)
        spans[project["name"]] = (t0, time.monotonic())
    monkeypatch.setattr(pipeline, "run_cycle", fake_cycle)

    worker_loop()
    assert _wait(lambda: len(spans) == 3), f"only {sorted(spans)} ran"
    (a0, a1), (b0, b1) = spans["alpha"], spans["beta"]
    assert a0 < b1 and b0 < a1          # genuinely overlapping


def test_a_desk_added_after_the_worker_started_gets_a_loop(
        fresh_db, may, worker_loop, monkeypatch):
    """Adding a project triggers it by name before its loop exists, so the
    wake has to reach the loop that starts new desks."""
    from harness import pipeline, worker
    ran = []

    async def fake_cycle(project, force=False):
        ran.append(project["name"])
    monkeypatch.setattr(pipeline, "run_cycle", fake_cycle)

    worker_loop()
    assert _wait(lambda: "may" in ran)
    fresh_db.create_project("roan", "example/roan")
    worker.trigger("roan")
    assert _wait(lambda: "roan" in ran), \
        "the new desk waited for the poll interval instead of its trigger"
