"""Run import workers in their own process:  python -m app.worker

Use this in production with JOB_MODE=external on the web process, so Stockfish work never
competes with page requests for the web process's time. The worker lowers its own CPU
priority (nice 10) so the web server stays snappy even when every core is analysing.

You can run more than one worker process (e.g. one per 2 CPU cores); they share the queue
in the database and never take the same job.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import socket
import threading

from .config import load_settings
from .jobs import JobQueue
from .observability import init_sentry
from .store import Store
from .sync import Importer, Jobs


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    init_sentry(settings, "worker")
    with contextlib.suppress(AttributeError, OSError):
        os.nice(int(os.getenv("WORKER_NICE", "10")))
    store = Store(settings.db_path)
    jobs = Jobs(JobQueue(settings.db_path), Importer(store, settings), settings)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    # Several worker processes may share the queue, so only re-queue jobs whose heartbeat has gone stale.
    jobs.start(name=f"worker@{socket.gethostname()}:{os.getpid()}", orphan_check=False)
    logging.getLogger("plateau.worker").info("worker started with %s threads", settings.max_concurrent_imports)
    stop.wait()
    jobs.stop()


if __name__ == "__main__":
    main()
