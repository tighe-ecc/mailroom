"""JSON-backed runtime settings + last-poll telemetry.

State file lives next to the SQLite DB (``~/Mailroom/.mailroom/settings.json``)
and stores two things:

- ``poll_interval_seconds`` — user-tunable minimum gap between background
  carrier polls. The launchd agent fires on its own cadence (see
  ``scripts/com.tighe.mailroom.poll.plist``); ``poll_once`` checks this
  setting and early-exits when the gap hasn't elapsed yet. That keeps the
  preference change effective immediately, without re-loading launchd.
- ``last_poll_at`` — ISO-8601 timestamp written by ``poll_once`` whenever it
  actually runs. The dashboard surfaces this so the user can see at a glance
  whether the background updater is alive.

JSON beats a SQLite row here because both the poller subprocess and the
FastAPI process need a low-friction read; a flat file is enough and avoids
schema churn for two scalars.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import db


_LOCK = threading.Lock()

DEFAULT_POLL_INTERVAL_SECONDS = 1800   # 30 minutes — matches the shipped plist
MIN_POLL_INTERVAL_SECONDS = 60
MAX_POLL_INTERVAL_SECONDS = 24 * 3600  # cap at one day so a typo doesn't disable polling


def settings_path(db_path: Path | None = None) -> Path:
    return (db_path or db.default_db_path()).parent / "settings.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read(db_path: Path | None = None) -> dict:
    path = settings_path(db_path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write(data: dict, db_path: Path | None = None) -> None:
    path = settings_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename for atomicity — the poller and the FastAPI process
    # read this file independently and a partial write would corrupt both.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def get_poll_interval_seconds(db_path: Path | None = None) -> int:
    with _LOCK:
        raw = _read(db_path).get("poll_interval_seconds")
    try:
        return _clamp_interval(int(raw))
    except (TypeError, ValueError):
        return DEFAULT_POLL_INTERVAL_SECONDS


def set_poll_interval_seconds(seconds: int, db_path: Path | None = None) -> int:
    """Persist the new interval. Returns the clamped value actually written."""
    clamped = _clamp_interval(seconds)
    with _LOCK:
        data = _read(db_path)
        data["poll_interval_seconds"] = clamped
        _write(data, db_path)
    return clamped


def _clamp_interval(seconds: int) -> int:
    return max(MIN_POLL_INTERVAL_SECONDS, min(int(seconds), MAX_POLL_INTERVAL_SECONDS))


def get_last_poll_at(db_path: Path | None = None) -> str | None:
    with _LOCK:
        return _read(db_path).get("last_poll_at")


def record_poll_at(when: str | None = None, db_path: Path | None = None) -> str:
    """Stamp ``last_poll_at``; the dashboard reads this for the "Last updated"
    chip and ``poll_once`` reads it to enforce the user's interval."""
    when = when or _now()
    with _LOCK:
        data = _read(db_path)
        data["last_poll_at"] = when
        _write(data, db_path)
    return when


# Surfaced from the launchd plist so the dashboard can be transparent about
# the *upper-bound* delay between updates: even if the user picks a short
# interval, polls won't fire more often than the plist's StartInterval.
# Override via env var when the plist is customized.
def daemon_tick_seconds() -> int:
    raw = os.environ.get("MAILROOM_POLL_TICK")
    if raw:
        try:
            return _clamp_interval(int(raw))
        except (TypeError, ValueError):
            pass
    return DEFAULT_POLL_INTERVAL_SECONDS
