"""Background worker: runs pipeline cycles on an interval in its own thread.

The worker gets its own thread and one long-lived event loop, so long
git/pytest runs never block the web GUI. Inside that loop each desk has its
own wake loop — its own event and its own interval — so an action on an idle
desk is served straight away instead of queueing behind another desk's wave.
Alongside them runs one loop for the section's shared chores: Harry's
directives and rulings, housekeeping and the stand-up clock. The GUI talks to
the worker through the database and `trigger()`.
"""
import asyncio
import threading
import traceback

from . import config, db, housekeeping, pipeline


_state = {"last_cycle": "", "thread": None,
          "draining": False,   # SIGTERM received: finish in-flight, start nothing
          "loop": None,        # the worker's event loop, once it is up
          "chores": None,      # asyncio.Event waking the shared-chores loop
          "desks": {},         # project name -> {"event": Event, "task": Task}
          "busy": set()}       # what is mid-cycle right now


HEARTBEAT_STALE_S = 45 * 60
READY_REWAKE_S = 5


def status() -> dict:
    age = db.heartbeat_age_seconds()
    alive = _state["thread"] is not None and _state["thread"].is_alive()
    return {"running": bool(_state["busy"]), "last_cycle": _state["last_cycle"],
            "alive": alive, "heartbeat_age": age,
            "draining": _state["draining"],
            "stale": alive and age is not None and age > HEARTBEAT_STALE_S}


def draining() -> bool:
    return _state["draining"]


def live_runs() -> int:
    with db.conn() as c:
        return c.execute("SELECT COUNT(*) FROM runs WHERE finished_at IS NULL"
                         ).fetchone()[0]


def request_drain(on_done=None, timeout_s: float | None = None) -> None:
    """Stop starting agent runs; let the ones in flight finish.

    Called from the SIGTERM path. `agents.run_agent` refuses to start while
    draining (raising AgentStalled, which every caller already treats as
    "pause, resume later"), every desk loop exits after its current cycle,
    and a watcher thread calls `on_done` once the worker thread has ended
    or `timeout_s` has passed. Nothing is marked failed: whatever is still
    running at the deadline gets closed by restart recovery as before."""
    if _state["draining"]:
        return
    _state["draining"] = True
    n = live_runs()
    db.log_event(f"Draining for restart: {n} run(s) in flight, starting no "
                 "more" if n else "Draining for restart: nothing in flight")
    _wake()  # every idle loop wakes and exits straight away

    def _watch():
        t = _state["thread"]
        if t is not None:
            t.join(timeout_s if timeout_s is not None else config.DRAIN_TIMEOUT_S)
            if t.is_alive():
                db.log_event(f"Drain timed out after {config.DRAIN_TIMEOUT_S}s "
                             f"with {live_runs()} run(s) still live", "warn")
            else:
                db.log_event("Drained — safe to restart")
        if on_done:
            on_done()
    threading.Thread(target=_watch, name="harness-drain", daemon=True).start()


def drain(timeout_s: float | None = None) -> bool:
    """Blocking form for callers that just want to wait. True if clean."""
    done = threading.Event()
    request_drain(on_done=done.set, timeout_s=timeout_s)
    done.wait()
    t = _state["thread"]
    return t is None or not t.is_alive()


def trigger(project: str | None = None) -> None:
    """Wake the worker now. Named a desk, only that desk's loop is woken.

    Human-initiated, so the cycle it starts runs even outside active hours.
    A desk already mid-cycle is not run twice over: its loop notes the wake
    and comes straight back round when the cycle it is in ends."""
    names = ([project] if project
             else [p["name"] for p in db.all_projects(enabled_only=True)])
    for n in names:
        db.set_setting(f"force_cycle.{n}", "1")
    if not project:
        db.set_setting("force_cycle", "1")   # the stand-up is section-wide
    _wake(project or None)


def _wake(project: str | None = None) -> None:
    """Set a wake event from any thread. No project means everything.

    Nothing to do if the worker's loop is not up (or is on its way down):
    what was asked for is recorded in the database, and every loop's first
    pass reads it."""
    loop = _state["loop"]
    if loop is None:
        return
    try:
        loop.call_soon_threadsafe(_set_wake, project)
    except RuntimeError:
        pass


def _set_wake(project: str | None) -> None:
    """Runs on the worker's loop, so the events can be plain asyncio ones."""
    desk = _state["desks"].get(project) if project else None
    if desk is not None and not desk["task"].done():
        desk["event"].set()
        return
    # No desk of that name yet (a project just added) or no name at all:
    # the chores loop is what notices new desks, so wake it too.
    if _state["chores"] is not None:
        _state["chores"].set()
    if project is None:
        for d in _state["desks"].values():
            d["event"].set()


def _wait_seconds(more: bool) -> float:
    """How long a loop sleeps before its next pass.

    Wakes early if paused_until expires before the normal interval — this is
    what resumes stalled work as soon as API limits reset."""
    wait = config.POLL_INTERVAL_MINUTES * 60
    if more:
        # A desk can go on without anyone's click: come straight back.
        # This is what makes the section run until the board is clear.
        wait = READY_REWAKE_S
    paused = db.paused_until()
    if paused:
        from datetime import datetime, timezone
        until = datetime.strptime(paused, "%Y-%m-%dT%H:%M:%SZ") \
            .replace(tzinfo=timezone.utc)
        secs = (until - datetime.now(timezone.utc)).total_seconds() + 60
        wait = max(60, min(wait, secs))
    return wait


async def _sleep_until_wake(event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(event.wait(), seconds)
    except asyncio.TimeoutError:
        pass


async def _desk_loop(name: str, event: asyncio.Event) -> None:
    """One desk's wake loop: its own event, its own interval.

    Only ever one cycle per desk at a time — the loop is the desk's lock —
    so two triggers on the same desk cannot put two waves in the same
    worktree pool. Nothing here waits on another desk."""
    while not _state["draining"]:
        event.clear()
        project = db.get_project(name)
        if project is None or not project["enabled"]:
            return   # desk gone or switched off; the chores loop forgets it
        more = False
        _state["busy"].add(name)
        db.touch_heartbeat()
        try:
            if not db.maintenance():   # idle until the operator clears it
                force = db.get_setting(f"force_cycle.{name}") == "1"
                db.set_setting(f"force_cycle.{name}", "")
                # A direction typed at this desk is acted on before the
                # cycle it woke chooses any work. Harry's rulings stay with
                # the chores loop: run_cycle puts the desk's own questions
                # to him as it goes, and doubling that up here would spend a
                # ruling run — and one of his two passes — for nothing.
                await pipeline.process_directives()
                await pipeline.run_cycle(project, force=force)
                more = not db.paused_until() and pipeline.work_ready(project)
        except pipeline.AgentStalled:
            # The pause (API limits) or the drain is global state that
            # run_agent checks, so the other desks stop starting new work
            # by themselves — no need to cancel them here.
            pass
        except Exception:
            db.log_event(f"Cycle for {name} crashed:\n"
                         + traceback.format_exc()[-1500:], "error", project=name)
        finally:
            _state["busy"].discard(name)
        _state["last_cycle"] = db.now()
        db.touch_heartbeat()
        if _state["draining"]:
            return
        await _sleep_until_wake(event, _wait_seconds(more))


def _supervise() -> None:
    """Give every enabled desk a wake loop; forget the ones that have ended."""
    for name, desk in list(_state["desks"].items()):
        if desk["task"].done():
            del _state["desks"][name]
    for p in db.all_projects(enabled_only=True):
        if p["name"] in _state["desks"]:
            continue
        event = asyncio.Event()
        _state["desks"][p["name"]] = {
            "event": event,
            "task": asyncio.create_task(_desk_loop(p["name"], event),
                                        name=f"desk-{p['name']}")}


async def _chores_loop() -> None:
    """The section's shared concerns, which stay section-wide: Harry's
    directives and rulings, housekeeping, the stand-up clock — plus starting
    a wake loop for each desk."""
    event = _state["chores"]
    while not _state["draining"]:
        event.clear()
        _state["busy"].add("chores")
        db.touch_heartbeat()
        try:
            # First, so a new desk's own loop starts without waiting on
            # anything the section owes Harry.
            _supervise()
            if not db.maintenance():
                force = db.get_setting("force_cycle") == "1"
                db.set_setting("force_cycle", "")
                await pipeline.process_directives()
                await pipeline.process_questions()
                if housekeeping.due():
                    await housekeeping.run(allow_agent=not db.paused_until())
                # Stand-up has its own clock, so a long cycle (or a restart
                # mid-cycle) can never starve it.
                if pipeline.standup_due():
                    await pipeline.run_standup(force=force)
        except Exception:
            db.log_event("Worker chores crashed:\n"
                         + traceback.format_exc()[-1500:], "error")
        finally:
            _state["busy"].discard("chores")
        _state["last_cycle"] = db.now()
        db.touch_heartbeat()
        if _state["draining"]:
            return
        await _sleep_until_wake(event, _wait_seconds(False))


async def _amain() -> None:
    _state["loop"] = asyncio.get_running_loop()
    _state["chores"] = asyncio.Event()
    _state["desks"] = {}
    _state["busy"] = set()
    try:
        try:
            await _chores_loop()
        except Exception:   # only the loop's own scaffolding can get here
            db.log_event("Worker chores loop stopped:\n"
                         + traceback.format_exc()[-1500:], "error")
        # Draining is finished only once every desk loop is idle too.
        tasks = [d["task"] for d in _state["desks"].values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        _state["loop"] = None


def _loop() -> None:
    asyncio.run(_amain())


def recover_after_restart() -> None:
    """Close runs orphaned by the previous process and requeue their items.

    Makes unattended restarts (deploys, watchtower auto-updates, crashes)
    self-healing: no manual tidy-up required."""
    with db.conn() as c:
        n = c.execute(
            "UPDATE runs SET ok = 0, finished_at = ?, "
            "summary = ? WHERE finished_at IS NULL",
            (db.now(), db.ORPHANED_SUMMARY)).rowcount
    requeued = []
    for p in db.all_projects():
        for it in db.items_by_status(p["name"], "working"):
            db.update_item(p["name"], it["kind"], it["number"],
                           status="approved")
            requeued.append(f"{p['name']} {it['kind']}#{it['number']}")
    with db.conn() as c:
        m = c.execute("UPDATE releases SET status = 'proposed' "
                      "WHERE status = 'merging'").rowcount
    if m:
        db.log_event(f"Restart recovery: {m} release(s) returned to proposed "
                     "(merge was interrupted; approve again)")
    if n or requeued:
        db.log_event(f"Restart recovery: closed {n} orphaned run(s)"
                     + (f", requeued {', '.join(requeued)}" if requeued else ""))


def start() -> None:
    if _state["thread"] is None:
        recover_after_restart()
        t = threading.Thread(target=_loop, name="harness-worker", daemon=True)
        _state["thread"] = t
        t.start()
        db.log_event("Worker started")
