"""Watched-folder pipeline: parses dropped .eml files and writes rows to SQLite.

Drop zone: ~/Mailroom/ (the folder you bookmark in Finder's sidebar).
Internal archive: ~/Mailroom/.mailroom/{processed,unrecognized,failed}/ — the
dot-prefix makes it hidden in Finder so the visible folder stays clean.
"""

from __future__ import annotations

import email
import email.policy
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

# Serializes process_inbox() calls so the watcher and HTTP upload endpoint don't race
# on the same file.
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


def _load_eml(path: Path) -> tuple[str, str, str]:
    with path.open("rb") as f:
        msg = email.message_from_binary_file(f, policy=email.policy.default)
    subject = (msg.get("Subject") or "").strip()
    sender = (msg.get("From") or "").strip()
    body = _body_text(msg)
    return subject, sender, body


def _apply(parsed: parser.ParsedEmail, sender_domain: str | None = None) -> tuple[str, int]:
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
    promised_delivery_date = _resolve_delivery_estimate(parsed)

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
            ordered_date=parsed.ordered_date,
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
            ordered_date=parsed.ordered_date,
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


def _resolve_delivery_estimate(parsed: parser.ParsedEmail) -> str | None:
    """Compute promised_delivery_date from a relative lead-time phrase.

    Vendors regularly quote "6-8 weeks" or "ships in 5 business days" instead
    of an absolute delivery date. The parser extracts that as ``lead_time_days``
    (upper bound of the range, expressed in calendar days); we anchor it to
    parsed.ordered_date — which for order_confirmation emails is the moment
    the order was placed — to produce a concrete date the dashboard can show.
    """
    if parsed.promised_delivery_date:
        return parsed.promised_delivery_date
    if not parsed.lead_time_days or not parsed.ordered_date:
        return None
    try:
        base = datetime.fromisoformat(parsed.ordered_date).date()
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


def _process_inbox_locked() -> dict[str, int]:
    dirs = _ensure_dirs()
    summary = {
        "seen": 0,
        "created": 0,
        "updated": 0,
        "unrecognized": 0,
        "failed": 0,
    }

    for path in sorted(dirs["root"].iterdir()):
        if path.is_dir() or path.suffix.lower() != ".eml":
            continue
        summary["seen"] += 1
        try:
            subject, sender, body = _load_eml(path)
            if not body:
                raise ValueError("could not extract body text from .eml")
            parsed = parser.parse_email(subject, sender, body)
            if not parsed.is_actionable:
                shutil.move(str(path), str(dirs["unrecognized"] / path.name))
                summary["unrecognized"] += 1
                log.info(
                    "unrecognized email %s (kind=%s conf=%.2f)",
                    path.name,
                    parsed.kind,
                    parsed.confidence,
                )
                continue
            sender_domain = db.extract_sender_domain(sender)
            action, row_id = _apply(parsed, sender_domain=sender_domain)
            summary[action] += 1
            shutil.move(str(path), str(dirs["processed"] / path.name))
            log.info("%s row %s from %s: %s", action, row_id, path.name, asdict(parsed))
        except Exception:
            summary["failed"] += 1
            err = traceback.format_exc()
            log.exception("failed to process %s", path.name)
            try:
                shutil.move(str(path), str(dirs["failed"] / path.name))
                (dirs["failed"] / f"{path.name}.error.txt").write_text(err)
            except Exception:
                log.exception("also failed to move %s to failed/", path.name)
    return summary


def pending_count() -> int:
    """How many .eml files are waiting in the inbox root."""
    root = inbox_dir()
    if not root.exists():
        return 0
    return sum(1 for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".eml")
