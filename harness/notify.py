"""Push notifications via ntfy (JSON publishing).

Fire-and-forget: a notification failure must never break pipeline work.
Disabled unless a topic is configured. `actions` become tappable buttons on
the phone; since the GUI is tailnet-only, the phone performs the HTTP action
itself over the tailnet — nothing is exposed publicly.
"""
import json
import urllib.request

from . import config

PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5}


def send(title: str, message: str, priority: str = "default",
         tags: str = "", click_path: str = "",
         actions: list[dict] | None = None) -> None:
    """actions: [{"label": str, "path": str, "body": str}] — at most 3;
    each becomes an http POST button against PUBLIC_URL + path."""
    if not config.NTFY_TOPIC:
        return
    try:
        payload = {
            "topic": config.NTFY_TOPIC,
            "title": title[:200],
            "message": message[:4000],
            "priority": PRIORITY.get(priority, 3),
        }
        if tags:
            payload["tags"] = tags.split(",")
        if click_path and config.PUBLIC_URL:
            payload["click"] = config.PUBLIC_URL.rstrip("/") + click_path
        if actions and config.PUBLIC_URL:
            payload["actions"] = [{
                "action": "http",
                "label": a["label"][:30],
                "url": config.PUBLIC_URL.rstrip("/") + a["path"],
                "method": "POST",
                "headers": {"Content-Type":
                            "application/x-www-form-urlencoded"},
                "body": a["body"],
                "clear": True,
            } for a in actions[:3]]
        req = urllib.request.Request(
            config.NTFY_URL.rstrip("/"),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # never let notifications take down real work
