"""FastAPI + Tailwind/DaisyUI dashboard for Mailroom.

Run: .venv/bin/uvicorn app:app --host 127.0.0.1 --port 47821
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import re

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mailroom import db, easypost, inbox, poll, scrape, settings as mr_settings, watcher

sys.path.insert(0, str(Path(__file__).resolve().parent))
from feedback import note as _feedback_note  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=ROOT / "templates")


STATUS_BADGE = {
    "ordered": "ghost",
    "confirmed": "ghost",
    "in_fulfillment": "ghost",
    "pre_transit": "info",
    "in_transit": "info",
    "out_for_delivery": "warning",
    "delivered": "success",
    "received": "success",
    "available_for_pickup": "info",
    "return_to_sender": "error",
    "failure": "error",
    "cancelled": "error",
    "error": "error",
    "unknown": "ghost",
}


def _pretty_date(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%a %b %-d")
    except ValueError:
        return value


def _pretty_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%b %-d, %-I:%M %p")
    except ValueError:
        return value


def _status_class(status: str | None) -> str:
    return STATUS_BADGE.get(status or "unknown", "ghost")


templates.env.filters["pretty_date"] = _pretty_date
templates.env.filters["pretty_time"] = _pretty_time
templates.env.filters["status_class"] = _status_class
templates.env.filters["display_status"] = easypost.display_status
templates.env.filters["tracking_url_for"] = scrape.tracking_url_for


_STATIC_DIR = ROOT / "static"


def _static_v(name: str) -> str:
    """Content-hash cache buster for a static asset.

    Templates reference `?v={{ static_v('foo.js') }}` so the import URL changes
    whenever the file's contents change. Without this, browsers cache the
    bundled JS by URL and won't pick up kit updates until the user hard-refreshes.
    Multi-user deployments (where every user does `git pull` and expects things
    to just work) need this to be automatic.

    Hashed at module load — static files don't change at runtime, only across
    restarts. Falls back to an empty string if the file is missing so the
    caller's URL stays valid even if a refactor renames an asset.
    """
    try:
        return hashlib.md5((_STATIC_DIR / name).read_bytes()).hexdigest()[:10]
    except OSError:
        return ""


templates.env.globals["static_v"] = _static_v


# Brief delay before the in-process poller's first fire, so we don't compete
# with uvicorn / the inbox watcher for startup CPU.
_POLL_TASK_STARTUP_DELAY_SECONDS = 8


async def _periodic_poll_loop() -> None:
    """In-process poll driver — runs on every GUI start, ticks forever.

    Why this exists: launchd's ``StartInterval`` on ``com.tighe.mailroom.poll``
    is documented as best-effort, and in practice it silently drops fires
    across sleep cycles, App Nap, and Low Power Mode. The dashboard chip then
    goes "stale" even though the app is alive. We can't fix launchd, so we
    route around it: as long as the GUI process is up, this task drives the
    poller on the same cadence the plist *wants*.

    The launchd agent stays installed as a backup — both callers share
    ``poll._interval_elapsed()``, so they coexist without double-polling.
    Whoever wakes up first runs the carrier poll; the other sees the gate
    closed and skips it.
    """
    # Sleep a bit at startup so the first fire doesn't pile onto uvicorn's
    # initial request handling. Use settings.daemon_tick_seconds() per-tick so
    # changes to MAILROOM_POLL_TICK take effect on the next loop iteration
    # without needing a GUI restart.
    try:
        await asyncio.sleep(_POLL_TASK_STARTUP_DELAY_SECONDS)
        while True:
            tick = mr_settings.daemon_tick_seconds()
            started = time.monotonic()
            logging.info("in-process poll tick start (interval=%ss)", tick)
            try:
                # Run synchronously off the event loop — poll_once() does
                # blocking I/O (sqlite, HTTP) that would stall the GUI's
                # request handlers if run inline. asyncio.to_thread() keeps
                # the event loop responsive.
                summary = await asyncio.to_thread(poll.poll_once)
                elapsed = time.monotonic() - started
                logging.info(
                    "in-process poll tick done in %.2fs: %s", elapsed, summary
                )
            except Exception:
                # One bad poll must not kill the task — log and keep ticking.
                # The launchd agent is a secondary backstop if this loop ever
                # silently wedges, but we don't want it to wedge here.
                elapsed = time.monotonic() - started
                logging.exception(
                    "in-process poll tick failed after %.2fs", elapsed
                )
            await asyncio.sleep(tick)
    except asyncio.CancelledError:
        # Clean shutdown — lifespan is tearing down. Re-raise so the task is
        # marked cancelled rather than completed.
        logging.info("in-process poll task cancelled")
        raise


@asynccontextmanager
async def _lifespan(app: FastAPI):
    db.init_schema()
    observer = watcher.start()
    poll_task = asyncio.create_task(_periodic_poll_loop(), name="mailroom-poll")
    try:
        yield
    finally:
        poll_task.cancel()
        try:
            await poll_task
        except (asyncio.CancelledError, Exception):
            # Cancellation is expected; any other exception was already logged
            # by the loop itself. Don't let teardown raise.
            pass
        watcher.stop(observer)


app = FastAPI(title="Mailroom", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


# --- feedback endpoint (FastAPI) -------------------------------------------
# Phase 2 trigger: set FEEDBACK_ROUTINE_ID and FEEDBACK_ROUTINE_TOKEN env vars
# after registering the routine (see DEPLOY.md step 4d). With them unset the
# endpoint still writes feedback.md and git-syncs; only the expedite trigger
# is no-op'd.

_FEEDBACK_LOG = logging.getLogger(__name__)

_EXPEDITE_LOCKFILE = Path.home() / "Mailroom" / ".mailroom" / ".feedback-agent.pid"
_EXPEDITE_LOG_FILE = Path("/tmp/mailroom-feedback-agent.log")
_EXPEDITE_PROMPT_FILE = ROOT / "feedback" / "drain_prompt.md"


def _git_sync_feedback() -> None:
    repo = str(ROOT)
    try:
        subprocess.run(["git", "-C", repo, "add", "feedback"], check=True, capture_output=True, text=True, timeout=10)
        if subprocess.run(["git", "-C", repo, "diff", "--cached", "--quiet"], timeout=10).returncode == 0:
            return
        subprocess.run(["git", "-C", repo, "commit", "-m", "feedback: sync new entry"], check=True, capture_output=True, text=True, timeout=10)
        subprocess.run(["git", "-C", repo, "push"], check=True, capture_output=True, text=True, timeout=30)
    except subprocess.CalledProcessError as exc:
        _FEEDBACK_LOG.warning("feedback git-sync failed: %s\n%s", exc, exc.stderr)
    except Exception as exc:
        _FEEDBACK_LOG.warning("feedback git-sync error: %s", exc)


def _find_claude_cli() -> str | None:
    """Locate the ``claude`` CLI binary for the headless feedback agent.

    The launchd-spawned FastAPI process gets a minimal PATH that typically
    omits ``~/.local/bin`` where the Claude Code CLI installer drops the
    binary. ``shutil.which('claude')`` alone returns None even though the
    user's shell finds it fine. We extend the search with known install
    locations and an env-var override so the kit works the same across
    different user installs.

    Order: MAILROOM_CLAUDE_BIN env var, shutil.which, then well-known paths.
    """
    override = os.environ.get("MAILROOM_CLAUDE_BIN")
    if override and os.access(override, os.X_OK):
        return override
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _expedite_pid_is_alive(pid: int) -> bool:
    """Best-effort liveness check via ``kill -0``."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but owned by someone else — treat as alive so we don't
        # stomp on an unrelated PID-recycled process.
        return True
    except OSError:
        return False
    return True


def _expedite_acquire_lock() -> bool:
    """Acquire the feedback-agent lockfile. Returns True if we now hold it.

    Three states: missing (write placeholder, proceed); present with a live
    PID (skip); present with a stale PID (clean, then proceed). We write our
    own PID first as a placeholder to close the check-then-spawn race window,
    then overwrite with the spawned child's PID after Popen succeeds.
    """
    try:
        _EXPEDITE_LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _FEEDBACK_LOG.warning("expedite: lockfile parent dir not writable: %s", exc)
        return False

    if _EXPEDITE_LOCKFILE.exists():
        try:
            raw = _EXPEDITE_LOCKFILE.read_text(encoding="utf-8").strip()
            existing_pid = int(raw) if raw else 0
        except (OSError, ValueError):
            existing_pid = 0
        if existing_pid and _expedite_pid_is_alive(existing_pid):
            _FEEDBACK_LOG.info(
                "expedite: feedback-agent already running (pid=%s); skipping",
                existing_pid,
            )
            return False
        _FEEDBACK_LOG.info(
            "expedite: stale lockfile (pid=%s gone); reclaiming", existing_pid or "?"
        )
        try:
            _EXPEDITE_LOCKFILE.unlink()
        except OSError as exc:
            _FEEDBACK_LOG.warning("expedite: failed to clear stale lockfile: %s", exc)
            return False

    try:
        _EXPEDITE_LOCKFILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:
        _FEEDBACK_LOG.warning("expedite: failed to write lockfile: %s", exc)
        return False
    return True


def _expedite_local() -> None:
    """Spawn a detached headless ``claude`` agent to drain feedback.md.

    Replaces the kit's remote-routine trigger. The mailroom user's feedback
    often cites parsed-email files under ``~/Mailroom/.mailroom/processed/*.eml``
    that a remote routine can't see, so the agent has to run on this machine
    with filesystem access to the repo. ``claude --add-dir <repo>`` gives the
    child process the working tree as its working set; auth is inherited from
    the user's local CLI install.

    Graceful degradation: missing CLI, missing prompt file, missing lock dir,
    or an in-flight drain all log and return — they never raise back into
    the FastAPI request, which has already been queued to the client.
    """
    claude_bin = _find_claude_cli()
    if not claude_bin:
        _FEEDBACK_LOG.warning(
            "expedite: 'claude' CLI not on PATH; skipping local agent spawn"
        )
        return

    if not _EXPEDITE_PROMPT_FILE.exists():
        _FEEDBACK_LOG.warning(
            "expedite: prompt file %s missing; skipping", _EXPEDITE_PROMPT_FILE
        )
        return
    try:
        prompt = _EXPEDITE_PROMPT_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        _FEEDBACK_LOG.warning("expedite: prompt file unreadable: %s", exc)
        return
    if not prompt.strip():
        _FEEDBACK_LOG.warning("expedite: prompt file is empty; skipping")
        return

    if not _expedite_acquire_lock():
        return

    # Detached: own session (so a uvicorn worker restart doesn't kill the
    # drain mid-flight via pgrp), stdin closed, stdout/stderr append-binary
    # to a shared log file.
    args = [claude_bin, "--print", "--add-dir", str(ROOT), prompt]
    try:
        log_fh = open(_EXPEDITE_LOG_FILE, "ab")
    except OSError as exc:
        _FEEDBACK_LOG.warning("expedite: cannot open log %s: %s", _EXPEDITE_LOG_FILE, exc)
        try:
            _EXPEDITE_LOCKFILE.unlink()
        except OSError:
            pass
        return
    try:
        proc = subprocess.Popen(
            args,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        _FEEDBACK_LOG.warning("expedite: failed to spawn claude: %s", exc)
        try:
            log_fh.close()
        except OSError:
            pass
        try:
            _EXPEDITE_LOCKFILE.unlink()
        except OSError:
            pass
        return
    finally:
        try:
            log_fh.close()
        except OSError:
            pass

    try:
        _EXPEDITE_LOCKFILE.write_text(str(proc.pid), encoding="utf-8")
    except OSError as exc:
        _FEEDBACK_LOG.warning("expedite: could not update lockfile with child pid: %s", exc)
    _FEEDBACK_LOG.info(
        "expedite: spawned local feedback-agent pid=%s (log=%s)",
        proc.pid, _EXPEDITE_LOG_FILE,
    )


@app.post("/feedback")
async def feedback(request: Request, background_tasks: BackgroundTasks) -> dict[str, bool]:
    payload = await request.json()
    expedited = bool(payload.get("expedite"))
    _feedback_note(
        payload.get("description", ""),
        type=payload.get("type", "bug"),
        title=payload.get("title") or None,
        tool=payload.get("tool") or None,
        url=payload.get("url") or None,
        expedited=expedited,
        path=ROOT / "feedback",
    )
    background_tasks.add_task(_git_sync_feedback)
    if expedited:
        background_tasks.add_task(_expedite_local)
    return {"ok": True}
# --- end feedback endpoint -------------------------------------------------


def _context(
    request: Request,
    include_delivered: bool = False,
    sort: str = "last_event_time",
    dir: str = "desc",
) -> dict[str, Any]:
    if sort not in db.SORTABLE_COLUMNS:
        sort = "last_event_time"
    if dir.lower() not in {"asc", "desc"}:
        dir = "desc"
    packages = db.list_packages(
        include_delivered=include_delivered, sort_by=sort, sort_dir=dir
    )
    counts = db.status_counts()
    return {
        "packages": packages,
        "counts": counts,
        "pre_shipment_count": db.pre_shipment_count(),
        "inbox_pending": inbox.pending_count(),
        "include_delivered": include_delivered,
        "sort_by": sort,
        "sort_dir": dir.lower(),
        "is_htmx": request.headers.get("hx-request") == "true",
        "poll_status": _poll_status(),
    }


def _poll_status() -> dict[str, Any]:
    """Background-poller health for the dashboard header chip.

    The user's ``poll_interval_seconds`` preference is a *minimum* gap — the
    actual upper bound is set by the launchd ``StartInterval`` (the daemon
    tick). If the user picks a 10-minute interval but the daemon only fires
    every 30 minutes, fresh data won't appear more often than 30 minutes,
    even though ``_interval_elapsed()`` is satisfied long before that. Stale
    detection has to use ``max(user_interval, daemon_tick)`` or the chip
    flips yellow prematurely and reports a non-bug — which is exactly the
    "background updater not running?" feedback we keep getting.
    """
    last = mr_settings.get_last_poll_at()
    interval = mr_settings.get_poll_interval_seconds()
    daemon_tick = mr_settings.daemon_tick_seconds()
    effective_period = max(interval, daemon_tick)
    age_seconds: int | None = None
    last_poll_local: str | None = None
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            now = datetime.now(last_dt.tzinfo) if last_dt.tzinfo else datetime.now()
            age_seconds = max(0, int((now - last_dt).total_seconds()))
            # Render an absolute local time so the user can verify the chip
            # without hovering for the tooltip.
            local_dt = last_dt.astimezone() if last_dt.tzinfo else last_dt
            last_poll_local = local_dt.strftime("%-I:%M %p")
        except ValueError:
            age_seconds = None
    # Stale = haven't heard from the poller within the effective period plus
    # a 60-second grace window (avoids a single-second flap right at the
    # edge). Flags a dead launchd daemon, a crashed poll script, or an unset
    # MAILROOM_DB path on the daemon side.
    stale = age_seconds is None or age_seconds > effective_period + 60
    return {
        "last_poll_at": last,
        "last_poll_local": last_poll_local,
        "age_seconds": age_seconds,
        "interval_seconds": interval,
        "daemon_tick_seconds": daemon_tick,
        "effective_period_seconds": effective_period,
        "stale": stale,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", _context(request))


@app.get("/packages", response_class=HTMLResponse)
def packages_fragment(
    request: Request,
    include_delivered: bool = False,
    sort: str = "last_event_time",
    dir: str = "desc",
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_packages.html", _context(request, include_delivered, sort, dir)
    )


@app.get("/packages/{row_id}/details", response_class=HTMLResponse)
def package_detail(request: Request, row_id: int) -> HTMLResponse:
    import json as _json
    pkg = db.get_package(row_id)
    if pkg is None:
        return HTMLResponse("<p class='text-error'>Not found.</p>", status_code=404)
    events: list[dict] = []
    if pkg.get("events_json"):
        try:
            events = _json.loads(pkg["events_json"])
        except _json.JSONDecodeError:
            events = []
    return templates.TemplateResponse(
        request, "_detail.html", {"pkg": pkg, "events": events}
    )


def _nn(s: str) -> str | None:
    s = s.strip()
    return s or None


@app.post("/add", response_class=HTMLResponse)
def add_row(
    request: Request,
    description: str = Form(...),
    tracking_number: str = Form(""),
    order_number: str = Form(""),
    vendor: str = Form(""),
    po_number: str = Form(""),
    carrier: str = Form(""),
    ordered_date: str = Form(""),
    promised_ship_date: str = Form(""),
    promised_delivery_date: str = Form(""),
) -> HTMLResponse:
    description = description.strip()
    tracking_number = tracking_number.strip()
    easypost_id = None
    status = "ordered"
    final_carrier = _nn(carrier)

    if tracking_number:
        snap = easypost.create_tracker(tracking_number, carrier=final_carrier)
        tracking_number = snap.tracking_number
        final_carrier = snap.carrier or final_carrier
        easypost_id = snap.easypost_id
        status = snap.status or "pre_transit"

    row_id = db.add_package(
        tracking_number=tracking_number or None,
        description=description,
        vendor=_nn(vendor),
        po_number=_nn(po_number),
        order_number=_nn(order_number),
        carrier=final_carrier,
        easypost_id=easypost_id,
        status=status,
        ordered_date=_nn(ordered_date),
        promised_ship_date=_nn(promised_ship_date),
        promised_delivery_date=_nn(promised_delivery_date),
    )

    if tracking_number and easypost_id:
        snap = easypost.retrieve_tracker(easypost_id)
        db.update_status(
            row_id=row_id,
            status=snap.status,
            est_delivery=snap.est_delivery,
            last_event=snap.last_event,
            last_event_time=snap.last_event_time,
            last_event_location=snap.last_event_location,
            events=snap.events,
            carrier=snap.carrier,
        )

    return templates.TemplateResponse(request, "_packages.html", _context(request))


@app.post("/packages/{row_id}/update", response_class=HTMLResponse)
def update_row(
    request: Request,
    row_id: int,
    description: str = Form(""),
    vendor: str = Form(""),
    order_number: str = Form(""),
    po_number: str = Form(""),
    tracking_number: str = Form(""),
    carrier: str = Form(""),
    status: str = Form(""),
    ordered_date: str = Form(""),
    promised_ship_date: str = Form(""),
    promised_delivery_date: str = Form(""),
) -> HTMLResponse:
    existing = db.get_package(row_id)
    if existing is None:
        return HTMLResponse("<p class='text-error'>Not found.</p>", status_code=404)

    new_tracking = tracking_number.strip() or None
    easypost_id = existing.get("easypost_id")
    final_carrier = _nn(carrier) or existing.get("carrier")
    final_status = _nn(status) or existing.get("status")
    had_tracking_before = bool(existing.get("tracking_number"))

    # If the user typed a tracking number that already belongs to another row,
    # the UNIQUE constraint would fire on UPDATE and surface as a 500. Detect
    # early and render an inline error pointing at the drag-to-merge workflow.
    if new_tracking and new_tracking != existing.get("tracking_number"):
        conflict = db.find_match(tracking_number=new_tracking)
        if conflict and conflict["id"] != row_id:
            label = conflict.get("description") or conflict.get("vendor") or f"row #{conflict['id']}"
            return _detail_with_error(
                request,
                row_id,
                f"Tracking number {new_tracking} is already used on “{label}”. "
                "Drag one row onto the other in the dashboard to combine them.",
            )

    if new_tracking and not had_tracking_before:
        try:
            snap = easypost.create_tracker(new_tracking, carrier=final_carrier)
            easypost_id = snap.easypost_id
            final_carrier = snap.carrier or final_carrier
            final_status = snap.status or final_status
        except Exception:
            logging.exception("EasyPost tracker creation failed for %s", new_tracking)

    db.update_package(
        row_id=row_id,
        description=_nn(description),
        vendor=_nn(vendor),
        order_number=_nn(order_number),
        po_number=_nn(po_number),
        tracking_number=new_tracking,
        carrier=final_carrier,
        status=final_status,
        easypost_id=easypost_id,
        ordered_date=_nn(ordered_date),
        promised_ship_date=_nn(promised_ship_date),
        promised_delivery_date=_nn(promised_delivery_date),
    )

    # Return refreshed detail fragment (swaps into #detail-content in the modal).
    return package_detail(request, row_id)


def _detail_with_error(request: Request, row_id: int, message: str) -> HTMLResponse:
    """Render the detail fragment with an inline error banner above the form."""
    import json as _json
    pkg = db.get_package(row_id)
    if pkg is None:
        return HTMLResponse("<p class='text-error'>Not found.</p>", status_code=404)
    events: list[dict] = []
    if pkg.get("events_json"):
        try:
            events = _json.loads(pkg["events_json"])
        except _json.JSONDecodeError:
            events = []
    return templates.TemplateResponse(
        request,
        "_detail.html",
        {"pkg": pkg, "events": events, "update_error": message},
    )


@app.post("/packages/{row_id}/receive", response_class=HTMLResponse)
def mark_received(request: Request, row_id: int) -> HTMLResponse:
    """Mark a delivered row as received — the user physically picked it up.
    Refreshes the table so the row drops out of the default view (received
    is hidden unless "Show received" is on)."""
    existing = db.get_package(row_id)
    if existing is None:
        return HTMLResponse("<p class='text-error'>Not found.</p>", status_code=404)
    db.update_package(row_id=row_id, status="received")
    return templates.TemplateResponse(request, "_packages.html", _context(request))


@app.delete("/packages/{row_id}", response_class=HTMLResponse)
def delete_row(request: Request, row_id: int) -> HTMLResponse:
    db.delete_package(row_id)
    return templates.TemplateResponse(request, "_packages.html", _context(request))


@app.post("/packages/{src_id}/merge/{dst_id}", response_class=HTMLResponse)
def merge_rows(request: Request, src_id: int, dst_id: int) -> HTMLResponse:
    """Combine two rows (drag-and-drop in the dashboard) and append a JSONL
    audit record so we can later analyze missed auto-dedup cases."""
    if src_id == dst_id:
        return HTMLResponse("cannot merge a row into itself", status_code=400)
    try:
        src_before, dst_before, merged = db.merge_packages(src_id, dst_id)
    except ValueError as e:
        return HTMLResponse(str(e), status_code=404)
    db.log_merge(src_before, dst_before, merged)
    return templates.TemplateResponse(request, "_packages.html", _context(request))


@app.post("/refresh", response_class=HTMLResponse)
def refresh(request: Request) -> HTMLResponse:
    # User asked for a refresh — bypass the configured interval gate.
    poll.poll_once(force=True)
    return templates.TemplateResponse(request, "_packages.html", _context(request))


@app.post("/settings/poll_interval", response_class=HTMLResponse)
def update_poll_interval(
    request: Request, poll_interval_minutes: int = Form(...)
) -> HTMLResponse:
    """Save the user's preferred poll frequency. Returns the OOB-swappable
    poll-status chip so the header updates without a full page reload."""
    seconds = max(1, poll_interval_minutes) * 60
    mr_settings.set_poll_interval_seconds(seconds)
    return templates.TemplateResponse(
        request,
        "_poll_status.html",
        {"poll_status": _poll_status()},
    )


@app.get("/poll_status", response_class=HTMLResponse)
def poll_status_fragment(request: Request) -> HTMLResponse:
    """Return the poll-status chip partial so it can self-refresh via HTMX."""
    return templates.TemplateResponse(
        request,
        "_poll_status.html",
        {"poll_status": _poll_status()},
    )


@app.post("/inbox/process", response_class=HTMLResponse)
def process_inbox(request: Request) -> HTMLResponse:
    inbox.process_inbox()
    return templates.TemplateResponse(request, "_packages.html", _context(request))


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9 ._\-()#&]+")


@app.post("/inbox/upload", response_class=HTMLResponse)
async def upload_eml(request: Request, file: UploadFile = File(...)) -> HTMLResponse:
    name = Path(file.filename or "upload.eml").name
    if not name.lower().endswith(".eml"):
        name = f"{name}.eml"
    name = _SAFE_FILENAME.sub("_", name)[:200] or "upload.eml"

    destination = inbox.inbox_dir() / name
    # If a file with the same name already exists, append a counter.
    if destination.exists():
        stem, suffix = destination.stem, destination.suffix
        i = 2
        while (candidate := destination.with_name(f"{stem} ({i}){suffix}")).exists():
            i += 1
        destination = candidate

    destination.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    destination.write_bytes(content)

    inbox.process_inbox()
    return templates.TemplateResponse(request, "_packages.html", _context(request))
