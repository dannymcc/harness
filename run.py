"""Harness entry point: start the background worker and the GUI."""
import signal
import subprocess
import uvicorn

from harness import config, worker
from harness.web.app import app


class _Server(uvicorn.Server):
    """SIGTERM drains before it stops.

    Docker (deploy, watchtower, compose down) sends SIGTERM. Instead of
    letting uvicorn exit at once — which killed every agent mid-run and
    left the items to restart recovery — the first SIGTERM asks the worker
    to finish what is in flight and start nothing new, keeps serving the
    GUI meanwhile, and shuts down once the worker has drained (or the
    drain timeout passes). A second SIGTERM, or SIGINT, exits at once."""

    def handle_exit(self, sig, frame):
        if sig == signal.SIGTERM and not worker.draining():
            worker.request_drain(on_done=lambda: setattr(self, "should_exit", True))
            return
        super().handle_exit(sig, frame)


def main() -> None:
    # Wire git pushes through gh's credentials (GH_TOKEN). Without this,
    # API calls work but `git push` dies on "could not read Username".
    try:
        subprocess.run(["gh", "auth", "setup-git"], check=True,
                       capture_output=True, timeout=30)
    except Exception as e:
        print(f"warning: gh auth setup-git failed: {e}")
    worker.start()
    _Server(uvicorn.Config(app, host=config.BIND_HOST, port=config.BIND_PORT)).run()


if __name__ == "__main__":
    main()
