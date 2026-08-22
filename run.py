"""Wilman entry point: start the background worker and the GUI."""
import uvicorn

from wilman import config, worker
from wilman.web.app import app


def main() -> None:
    worker.start()
    uvicorn.run(app, host=config.BIND_HOST, port=config.BIND_PORT)


if __name__ == "__main__":
    main()
