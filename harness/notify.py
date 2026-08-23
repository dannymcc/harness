"""Push notifications via ntfy.

Fire-and-forget: a notification failure must never break pipeline work, so
everything is swallowed after one short attempt. Disabled unless a topic is
configured.
"""
import urllib.request

from . import config


def send(title: str, message: str, priority: str = "default",
         tags: str = "", click_path: str = "") -> None:
    if not config.NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"{config.NTFY_URL.rstrip('/')}/{config.NTFY_TOPIC}",
            data=message.encode()[:4000],
            headers={
                "Title": title.encode("ascii", "ignore").decode()[:200],
                "Priority": priority,
                **({"Tags": tags} if tags else {}),
                **({"Click": config.PUBLIC_URL.rstrip("/") + click_path}
                   if click_path and config.PUBLIC_URL else {}),
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # never let notifications take down real work
