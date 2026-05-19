"""Watched-folder pipeline: parses dropped .eml files and writes rows to SQLite.

Drop zone: ~/Mailroom/ (the folder you bookmark in Finder's sidebar).
Internal archive: ~/Mailroom/.mailroom/{processed,unrecognized,failed}/ — the
dot-prefix makes it hidden in Finder so the visible folder stays clean.
"""

from __future__ import annotations

import contextlib
import email
import email.policy
import fcntl
import logging
import os
import shutil
import threading
import traceback
from dataclasses import asdict
from datetime import datetime, timedelta
from email.message import Message
from pathlib import Path

import html2text

from . import db, easypost, notify, parser, scrape

log = logging.getLogger(__name__)

# Serializes process_inbox() calls within a single process (FastAPI watcher,
# upload endpoint, manual trigger). The cross-process race — the launchd poll
# script and the FastAPI watcher both scanning the same inbox at the same time
# — is handled by an OS-level file lock acquired in `_process_inbox_locked`.
_PROCESS_LOCK = threading.Lock()

INBOX_ROOT_ENV = "MAILROOM_INBOX"
LEGACY_INBOX_ROOT_ENV = "PROCUREMENT_TRACKER_INBOX"
DEFAULT_INBOX = Path.home() / "Mailroom"
INTERNAL_SUBDIR = ".mailroom"  # hidden in Finder via dot-prefix


def inbox_dir() -> Path:
    override = os.environ.get(INBOX_ROOT_ENV) or os.environ.get(LEGACY_INBOX_ROOT_ENV)
    return Path(override).expanduser() if override else DEFAULT_INBOX


def _ensure_dirs() -> dict[str, Path]:
    root = inbox_dir()
    internal = root / INTERNAL_SUBDIR
    subdirs = {
        "root": root,
        "processed": internal / "processed",
        "unrecognized": internal / "unrecognized",
        "failed": internal / "failed",
    }
    for p in subdirs.values():
        p.mkdir(parents=True, exist_ok=True)
    return subdirs


def _body_text(msg: Message) -> str:
    """Extract a plain-text body from an email.Message, preferring text/plain."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get_content_disposition() == "attachment":
                continue
            if ctype == "text/plain":
                try:
                    plain_parts.append(part.get_content())
                except (LookupError, UnicodeDecodeError):
                    plain_parts.append(_decode_bytes(part))
            elif ctype == "text/html":
                try:
                    html_parts.append(part.get_content())
                except (LookupError, UnicodeDecodeError):
                    html_parts.append(_decode_bytes(part))
    else:
        ctype = msg.get_content_type()
        try:
            content = msg.get_content()
        except (LookupError, UnicodeDecodeError):
            content = _decode_bytes(msg)
        if ctype == "text/plain":
            plain_parts.append(content)
        elif ctype == "text/html":
            html_parts.append(content)

    if plain_parts:
        return "\n\n".join(plain_parts).strip()
    if html_parts:
        h2t = html2text.HTML2Text()
        h2t.ignore_images = True
        h2t.body_width = 0
        return "\n\n".join(h2t.handle(h) for h in html_parts).strip()
    return ""


def _decode_bytes(part: Message) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _load_eml(path: Path) -> tuple[str, str, str, str | None]:
    with path.open("rb") as f:
        msg = email.message_from_binary_file(f, policy=email.policy.default)
    subject = (msg.get("Subject") or "").strip()
    sender = (msg.get("From") or "").strip()
    body = _body_text(msg)
    email_date = _header_date(msg.get("Date"))
    return subject, sender, body, email_date


def _header_date(raw: str | None) -> str | None:
    """Parse an RFC 2822 Date header into a YYYY-MM-DD string.

    The email Date header is the authoritative timestamp for *when the email
    was sent*. For an order-confirmation email, that's the moment the vendor
    acknowledged the order — which we use as the order date in preference to
    LLM-extracted dates that often pick up promised-ship or quote-validity
    dates instead (StepperOnline confirmations e.g. include a "Sun Jul 5"
    validity date in the body that the LLM was using as ordered_date).
    """
    if not raw:
        return None
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt.date().isoformat()


def _apply(
    parsed: parser.ParsedEmail,
    sender_domain: str | None = None,
    email_date: str | None = None,
) -> tuple[str, int]:
    """Create or update a row from a ParsedEmail. Returns (action, row_id)."""
    existing = db.find_match(
        tracking_number=parsed.tracking_number,
        order_number=parsed.order_number,
        po_number=parsed.po_number,
        vendor=parsed.vendor,
        sender_domain=sender_domain,
    )

    status = _status_from_signal(parsed.status_signal, parsed.tracking_number)
    easypost_id = None
    carrier = parsed.carrier
    snap = None
    tracker_error: str | None = None
    ordered_date = _resolve_ordered_date(parsed, email_date)
    # _resolve_delivery_estimate reads parsed.ordered_date as its anchor; if the
    # email Date header gave us a better one, prefer that for the lead-time math
    # too so "6-8 weeks from when this email was sent" lines up with the date
    # we just decided to store.
    promised_delivery_date = _resolve_delivery_estimate(parsed, ordered_date)

    if parsed.tracking_number:
        prior_easypost = existing.get("easypost_id") if existing else None
        if not prior_easypost:
            try:
                snap = easypost.create_tracker(parsed.tracking_number, carrier=carrier)
                easypost_id = snap.easypost_id
                carrier = snap.carrier or carrier
                status = snap.status or status
            except Exception as e:
                log.exception(
                    "failed to register tracker %s with EasyPost", parsed.tracking_number
                )
                tracker_error = f"{type(e).__name__}: {e}"
                # Vendor said "shipped" but we can't track it (unsupported carrier, freight,
                # etc.). "pre_transit" would imply label-created-not-yet-picked-up; the
                # shipping email tells us the carrier already has it.
                if parsed.status_signal == "shipped":
                    status = "in_transit"

    if existing:
        old_status = existing.get("status")
        row_id = existing["id"]
        # Don't let a late-arriving order-confirmation email regress an
        # already-shipped row's status. (Inbox files sort alphabetically, so
        # "DigiKey has shipped..." can be processed before "Thank you for your
        # DigiKey order!" — without this guard the second email overwrites
        # status="delivered" with status="confirmed".)
        if db.is_status_regression(old_status, status):
            status = None
        db.update_package(
            row_id=row_id,
            tracking_number=parsed.tracking_number,
            order_number=parsed.order_number,
            description=parsed.item_description,
            vendor=parsed.vendor,
            po_number=parsed.po_number,
            carrier=carrier,
            easypost_id=easypost_id,
            status=status,
            ordered_date=ordered_date,
            promised_ship_date=parsed.promised_ship_date,
            promised_delivery_date=promised_delivery_date,
            tracking_url=parsed.tracking_url,
            sender_domain=sender_domain,
        )
        action = "updated"
    else:
        row_id = db.add_package(
            tracking_number=parsed.tracking_number,
            order_number=parsed.order_number,
            description=parsed.item_description,
            vendor=parsed.vendor,
            po_number=parsed.po_number,
            carrier=carrier,
            easypost_id=easypost_id,
            status=status,
            ordered_date=ordered_date,
            promised_ship_date=parsed.promised_ship_date,
            promised_delivery_date=promised_delivery_date,
            tracking_url=parsed.tracking_url,
            sender_domain=sender_domain,
        )
        old_status = None
        action = "created"

    if snap is not None:
        db.update_status(
            row_id=row_id,
            status=snap.status,
            est_delivery=snap.est_delivery,
            last_event=snap.last_event,
            last_event_time=snap.last_event_time,
            last_event_location=snap.last_event_location,
            events=snap.events,
            carrier=snap.carrier,
            easypost_id=snap.easypost_id,
        )
    elif tracker_error:
        db.set_tracker_error(row_id, tracker_error)
        fresh = db.get_package(row_id) or {}
        scraped_ok = scrape.apply_to_row(
            fresh,
            old_status=old_status,
            description=parsed.item_description,
        )
        if scraped_ok:
            # Scrape updated the row; don't re-fire status notification below.
            return action, row_id

    if status and status != old_status:
        notify.notify_status_change(
            description=parsed.item_description
            or (existing or {}).get("description")
            or parsed.order_number
            or "",
            old_status=old_status,
            new_status=status,
            location=snap.last_event_location if snap else None,
            vendor=parsed.vendor or (existing or {}).get("vendor"),
        )

    return action, row_id


# Trust the body-extracted ordered_date only when the LLM self-rated it this
# high. Anything less and the email Date header wins on order_confirmation
# emails (Date header = "when this email was sent", which for an order ack is
# effectively the order placement time). Tuned so the LLM has to be near-certain
# the body really labels the date as the order date — the failure mode we're
# guarding against is the LLM tagging a quote-validity or promised-ship date as
# ordered_date.
HIGH_BODY_DATE_CONFIDENCE = 0.9

# Plausibility window: an order_confirmation's ordered_date must be close to the
# email Date header (the moment the vendor acknowledged the order). Anything
# noticeably in the future is mechanically impossible; anything very far in the
# past is almost certainly a misparse. Catches DD/MM-vs-MM/DD format misreads
# the LLM can't self-diagnose: StepperOnline writes "Date Added: 07/05/2026"
# for an order placed May 7 (DD/MM); the LLM confidently parses it as July 5
# (MM/DD) with 0.95 confidence and would clear the confidence gate. The
# date-sanity gate catches it because Jul 5 sits 60 days in the future of the
# May 6 email header.
MAX_BODY_DATE_DAYS_FUTURE = 3    # acknowledgements going out before the order is rare
MAX_BODY_DATE_DAYS_PAST = 60     # generous: backfilled / forwarded confirmations do exist


def _resolve_ordered_date(parsed: parser.ParsedEmail, email_date: str | None) -> str | None:
    """Choose the ordered_date to write.

    For shipping confirmations: use the LLM's body extraction — the email Date
    header is the ship date, not the order date.

    For order confirmations: default to the email Date header (a safe lower
    bound on when the order was placed). Promote the LLM's body extraction
    only when *both* the LLM is near-certain (ordered_date_confidence) and the
    extracted date falls within a plausible window of the email Date header.
    """
    if parsed.kind != "order_confirmation":
        return parsed.ordered_date
    if (
        parsed.ordered_date
        and parsed.ordered_date_confidence >= HIGH_BODY_DATE_CONFIDENCE
        and _within_plausible_window(parsed.ordered_date, email_date)
    ):
        return parsed.ordered_date
    return email_date or parsed.ordered_date


def _within_plausible_window(body_date: str, email_date: str | None) -> bool:
    """True iff body_date is close enough to email_date that it could plausibly
    be the same order's placement date. Wide-open if email_date is unavailable
    — no anchor to compare against, so fall back to trusting the LLM."""
    if not email_date:
        return True
    try:
        b = datetime.fromisoformat(body_date).date()
        e = datetime.fromisoformat(email_date).date()
    except ValueError:
        return True
    delta_days = (b - e).days
    return -MAX_BODY_DATE_DAYS_PAST <= delta_days <= MAX_BODY_DATE_DAYS_FUTURE


def _resolve_delivery_estimate(
    parsed: parser.ParsedEmail, anchor_date: str | None
) -> str | None:
    """Compute promised_delivery_date from a relative lead-time phrase.

    Vendors regularly quote "6-8 weeks" or "ships in 5 business days" instead
    of an absolute delivery date. The parser extracts that as ``lead_time_days``
    (upper bound of the range, expressed in calendar days); we anchor it to
    ``anchor_date`` — typically the ordered_date we just resolved, which on
    order-confirmation emails comes from the Date header — to produce a
    concrete date the dashboard can show.
    """
    if parsed.promised_delivery_date:
        return parsed.promised_delivery_date
    if not parsed.lead_time_days or not anchor_date:
        return None
    try:
        base = datetime.fromisoformat(anchor_date).date()
    except ValueError:
        return None
    return (base + timedelta(days=parsed.lead_time_days)).isoformat()


def _status_from_signal(signal: str | None, tracking_number: str | None) -> str:
    if signal == "shipped" or (tracking_number and signal is None):
        return "pre_transit"
    if signal in {"ordered", "confirmed", "in_fulfillment"}:
        return signal
    return "ordered"


def process_inbox() -> dict[str, int]:
    """Scan the inbox directory, process every .eml, return a summary."""
    with _PROCESS_LOCK:
        return _process_inbox_locked()


@contextlib.contextmanager
def _inbox_file_lock(root: Path):
    """Serialize inbox processing across processes (launchd poll vs FastAPI watcher).

    Without this, both processes can enumerate the same .eml at the same time
    and race to `shutil.move` it — the loser raises FileNotFoundError and the
    summary reports inflated `failed` counts even though the file actually
    landed in `processed/`. The threading lock in this module only serializes
    callers in a single Python process.
    """
    internal = root / INTERNAL_SUBDIR
    internal.mkdir(parents=True, exist_ok=True)
    lockfile = internal / ".process.lock"
    fd = os.open(str(lockfile), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _process_inbox_locked() -> dict[str, int]:
    dirs = _ensure_dirs()
    summary = {
        "seen": 0,
        "created": 0,
        "updated": 0,
        "unrecognized": 0,
        "failed": 0,
    }

    with _inbox_file_lock(dirs["root"]):
        # Snapshot the directory listing once under the lock. Any file that
        # disappears between snapshot and processing (manual cleanup, finder
        # rename, etc.) is skipped silently — it's no longer this pass's job.
        paths = sorted(dirs["root"].iterdir())
        for path in paths:
            if path.is_dir() or path.suffix.lower() != ".eml":
                continue
            if not path.exists():
                # Vanished between snapshot and now — treat as already-handled
                # rather than failed.
                continue
            summary["seen"] += 1
            try:
                subject, sender, body, email_date = _load_eml(path)
                if not body:
                    raise ValueError("could not extract body text from .eml")
                parsed = parser.parse_email(subject, sender, body)
                if not parsed.is_actionable:
                    _safe_move(path, dirs["unrecognized"] / path.name)
                    summary["unrecognized"] += 1
                    log.info(
                        "unrecognized email %s (kind=%s conf=%.2f)",
                        path.name,
                        parsed.kind,
                        parsed.confidence,
                    )
                    continue
                sender_domain = db.extract_sender_domain(sender)
                action, row_id = _apply(
                    parsed, sender_domain=sender_domain, email_date=email_date
                )
                summary[action] += 1
                _safe_move(path, dirs["processed"] / path.name)
                log.info("%s row %s from %s: %s", action, row_id, path.name, asdict(parsed))
            except FileNotFoundError:
                # Another process moved the file out from under us. Roll the
                # `seen` count back and move on — this isn't a failure.
                summary["seen"] -= 1
                log.info("inbox file %s vanished mid-process; skipping", path.name)
                continue
            except Exception:
                summary["failed"] += 1
                err = traceback.format_exc()
                log.exception("failed to process %s", path.name)
                try:
                    _safe_move(path, dirs["failed"] / path.name)
                    (dirs["failed"] / f"{path.name}.error.txt").write_text(err)
                except Exception:
                    log.exception("also failed to move %s to failed/", path.name)
    return summary


def _safe_move(src: Path, dst: Path) -> None:
    """`shutil.move` that tolerates a vanished source (another process beat us)."""
    try:
        shutil.move(str(src), str(dst))
    except FileNotFoundError:
        # Source already moved by a peer process — treat as success.
        log.info("source %s already moved by another process; skipping", src.name)


def pending_count() -> int:
    """How many .eml files are waiting in the inbox root."""
    root = inbox_dir()
    if not root.exists():
        return 0
    return sum(1 for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".eml")
