"""Harness entry point: start the background worker and the GUI."""
import subprocess
import uvicorn

from harness import config, worker
from harness.web.app import app


def main() -> None:
    # Wire git pushes through gh's credentials (GH_TOKEN). Without this,
    # API calls work but `git push` dies on "could not read Username".
    try:
        subprocess.run(["gh", "auth", "setup-git"], check=True,
                       capture_output=True, timeout=30)
    except Exception as e:
        print(f"warning: gh auth setup-git failed: {e}")
    worker.start()
    uvicorn.run(app, host=config.BIND_HOST, port=config.BIND_PORT)


if __name__ == "__main__":
    main()
