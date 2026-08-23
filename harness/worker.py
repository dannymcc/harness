"""Background worker: runs pipeline cycles on an interval in its own thread.

The worker gets its own thread + event loop so long git/pytest runs never
block the web GUI. The GUI talks to it via the database and a "run now"
event.
"""
import asyncio
import threading
import traceback

from . import config, db, housekeeping, pipeline

class _Maintenance(Exception):
    pass


_run_now = threading.Event()
_state = {"running": False, "last_cycle": "", "thread": None}


HEARTBEAT_STALE_S = 45 * 60


def status() -> dict:
    age = db.heartbeat_age_seconds()
    alive = _state["thread"] is not None and _state["thread"].is_alive()
    return {"running": _state["running"], "last_cycle": _state["last_cycle"],
            "alive": alive, "heartbeat_age": age,
            "stale": alive and age is not None and age > HEARTBEAT_STALE_S}


def trigger() -> None:
    # Human-initiated: the next cycle runs even outside active hours.
    db.set_setting("force_cycle", "1")
    _run_now.set()


def _loop() -> None:
    while True:
        _run_now.clear()
        _state["running"] = True
        db.touch_heartbeat()
        try:
            if db.maintenance():
                raise _Maintenance()
            force = db.get_setting("force_cycle") == "1"
            db.set_setting("force_cycle", "")
            if housekeeping.due():
                asyncio.run(housekeeping.run(allow_agent=not db.paused_until()))
            # Stand-up has its own clock and runs BEFORE the sweep, so a
            # long cycle (or a restart mid-cycle) can never starve it.
            if pipeline.standup_due():
                asyncio.run(pipeline.run_standup(force=force))
            asyncio.run(pipeline.run_all_cycles(force=force))
        except _Maintenance:
            pass  # maintenance mode: idle until the operator clears it
        except Exception:
            db.log_event("Worker cycle crashed:\n" + traceback.format_exc()[-1500:],
                         "error")
        _state["running"] = False
        _state["last_cycle"] = db.now()
        db.touch_heartbeat()
        # Wake early if paused_until expires before the normal interval —
        # this is what resumes stalled work as soon as limits reset.
        wait = config.POLL_INTERVAL_MINUTES * 60
        paused = db.paused_until()
        if paused:
            from datetime import datetime, timezone
            until = datetime.strptime(paused, "%Y-%m-%dT%H:%M:%SZ") \
                .replace(tzinfo=timezone.utc)
            secs = (until - datetime.now(timezone.utc)).total_seconds() + 60
            wait = max(60, min(wait, secs))
        _run_now.wait(timeout=wait)


def start() -> None:
    if _state["thread"] is None:
        t = threading.Thread(target=_loop, name="harness-worker", daemon=True)
        _state["thread"] = t
        t.start()
        db.log_event("Worker started")
