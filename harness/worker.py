"""Background worker: runs pipeline cycles on an interval in its own thread.

The worker gets its own thread + event loop so long git/pytest runs never
block the web GUI. The GUI talks to it via the database and a "run now"
event.
"""
import asyncio
import threading
import time
import traceback

from . import config, db, housekeeping, pipeline

class _Maintenance(Exception):
    pass


_run_now = threading.Event()
_state = {"running": False, "last_cycle": "", "thread": None,
          "ready": [],       # desks with signed-off work: next wake is quick
          "last_full": 0.0}  # monotonic time of the last full sweep


HEARTBEAT_STALE_S = 45 * 60
READY_REWAKE_S = 60


def status() -> dict:
    age = db.heartbeat_age_seconds()
    alive = _state["thread"] is not None and _state["thread"].is_alive()
    return {"running": _state["running"], "last_cycle": _state["last_cycle"],
            "alive": alive, "heartbeat_age": age,
            "stale": alive and age is not None and age > HEARTBEAT_STALE_S}


def trigger() -> None:
    # Human-initiated: the next cycle runs even outside active hours, and
    # over every desk (not just the ones queued for a quick re-wake).
    db.set_setting("force_cycle", "1")
    _state["ready"] = []
    _run_now.set()


def _loop() -> None:
    while True:
        _run_now.clear()
        _state["running"] = True
        more = False
        db.touch_heartbeat()
        try:
            if db.maintenance():
                raise _Maintenance()
            force = db.get_setting("force_cycle") == "1"
            db.set_setting("force_cycle", "")
            asyncio.run(pipeline.process_directives())
            # Harry rules on anything his people asked since the last wake,
            # before the desks plan again — nobody plans around an open
            # question that he could have settled.
            asyncio.run(pipeline.process_questions())
            if housekeeping.due():
                asyncio.run(housekeeping.run(allow_agent=not db.paused_until()))
            # Stand-up has its own clock and runs BEFORE the sweep, so a
            # long cycle (or a restart mid-cycle) can never starve it.
            if pipeline.standup_due():
                asyncio.run(pipeline.run_standup(force=force))
            # Quick re-wakes cover only the desks with signed-off work, but
            # never for longer than a poll interval: every desk still gets
            # its sync / triage / review / release check on the normal clock.
            only = _state["ready"] or None
            full_due = (time.monotonic() - _state["last_full"]
                        >= config.POLL_INTERVAL_MINUTES * 60)
            if force or full_due:
                only = None
            if only is None:
                _state["last_full"] = time.monotonic()
            ready = asyncio.run(pipeline.run_all_cycles(force=force, only=only))
            _state["ready"] = ready
            more = bool(ready)
        except _Maintenance:
            pass  # maintenance mode: idle until the operator clears it
        except Exception:
            _state["ready"] = []
            db.log_event("Worker cycle crashed:\n" + traceback.format_exc()[-1500:],
                         "error")
        _state["running"] = False
        _state["last_cycle"] = db.now()
        db.touch_heartbeat()
        # Wake early if paused_until expires before the normal interval —
        # this is what resumes stalled work as soon as limits reset.
        wait = config.POLL_INTERVAL_MINUTES * 60
        if more:
            # A desk has approved/assigned work ready to start: come straight
            # back rather than leaving it for the next poll.
            wait = READY_REWAKE_S
        paused = db.paused_until()
        if paused:
            from datetime import datetime, timezone
            until = datetime.strptime(paused, "%Y-%m-%dT%H:%M:%SZ") \
                .replace(tzinfo=timezone.utc)
            secs = (until - datetime.now(timezone.utc)).total_seconds() + 60
            wait = max(60, min(wait, secs))
        _run_now.wait(timeout=wait)


def recover_after_restart() -> None:
    """Close runs orphaned by the previous process and requeue their items.

    Makes unattended restarts (deploys, watchtower auto-updates, crashes)
    self-healing: no manual tidy-up required."""
    with db.conn() as c:
        n = c.execute(
            "UPDATE runs SET ok = 0, finished_at = ?, "
            "summary = 'orphaned by restart' WHERE finished_at IS NULL",
            (db.now(),)).rowcount
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
