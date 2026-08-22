"""Background worker: runs pipeline cycles on an interval in its own thread.

The worker gets its own thread + event loop so long git/pytest runs never
block the web GUI. The GUI talks to it via the database and a "run now"
event.
"""
import asyncio
import threading
import traceback

from . import config, db, housekeeping, pipeline

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
    _run_now.set()


def _loop() -> None:
    while True:
        _run_now.clear()
        _state["running"] = True
        db.touch_heartbeat()
        try:
            hourly = housekeeping.due()
            if hourly:
                asyncio.run(housekeeping.run(allow_agent=not db.paused_until()))
            asyncio.run(pipeline.run_all_cycles())
            if hourly:
                asyncio.run(pipeline.run_standup())
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
