"""Filesystem watcher that auto-processes .eml files the moment they land in inbox/.

Runs inside the FastAPI process via a lifespan context. Starts when uvicorn starts,
stops when uvicorn stops. No separate daemon to manage.

When a file is created or moved INTO the inbox root, we debounce briefly (the file
may still be writing) and then call inbox.process_inbox(). The inbox pipeline itself
already handles duplicates, unrecognized emails, and failures.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import inbox

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 1.5  # give Finder / Outlook time to finish writing the .eml


class _EmlHandler(FileSystemEventHandler):
    """Fire process_inbox() on any .eml arrival, coalesced to at most once per window."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: threading.Timer | None = None

    def _schedule(self) -> None:
        with self._lock:
            if self._pending is not None:
                self._pending.cancel()
            self._pending = threading.Timer(DEBOUNCE_SECONDS, self._run)
            self._pending.daemon = True
            self._pending.start()

    def _run(self) -> None:
        try:
            summary = inbox.process_inbox()
            log.info("auto-processed inbox: %s", summary)
        except Exception:
            log.exception("auto-process failed")

    def _maybe_fire(self, path: str) -> None:
        if path.lower().endswith(".eml"):
            self._schedule()

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_fire(str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_fire(str(event.dest_path))


def start() -> Observer:
    """Begin watching the inbox directory. Returns the observer so the caller can stop it."""
    root: Path = inbox.inbox_dir()
    root.mkdir(parents=True, exist_ok=True)
    observer = Observer()
    observer.schedule(_EmlHandler(), str(root), recursive=False)
    observer.start()
    log.info("inbox watcher started on %s", root)
    return observer


def stop(observer: Observer) -> None:
    observer.stop()
    observer.join(timeout=3.0)
    log.info("inbox watcher stopped")
