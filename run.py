"""Harness entry point: start the background worker and the GUI."""
import signal
import subprocess
from pathlib import Path

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


def persist_claude_home() -> None:
    """Keep the Agent SDK's session transcripts on the data volume.

    A fix that is cut off mid-run resumes its session next cycle, but the
    SDK writes those transcripts under `~/.claude`, which in the container
    is the writable layer — a recreate throws them away and the resume dies
    on "No conversation found with session ID". Pointing `~/.claude` at
    `DATA_DIR/claude-home` puts them inside the existing `./data` mount, so
    no compose change is needed. Idempotent: main() runs on every boot.

    A real `~/.claude` directory is left alone — on a developer's machine
    that is their own Claude home, not ours to move into the project.
    """
    target = config.DATA_DIR / "claude-home"
    link = Path.home() / ".claude"
    try:
        if link.is_symlink():
            target.mkdir(parents=True, exist_ok=True)
            return
        if link.exists():
            print(f"warning: {link} is a real directory — leaving it in "
                  "place; agent sessions will not survive a container "
                  "recreate")
            return
        target.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target, target_is_directory=True)
    except OSError as e:
        print(f"warning: could not keep agent sessions in {target}: {e}")


def main() -> None:
    # Wire git pushes through gh's credentials (GH_TOKEN). Without this,
    # API calls work but `git push` dies on "could not read Username".
    try:
        subprocess.run(["gh", "auth", "setup-git"], check=True,
                       capture_output=True, timeout=30)
    except Exception as e:
        print(f"warning: gh auth setup-git failed: {e}")
    persist_claude_home()
    worker.start()
    _Server(uvicorn.Config(app, host=config.BIND_HOST, port=config.BIND_PORT)).run()


if __name__ == "__main__":
    main()
