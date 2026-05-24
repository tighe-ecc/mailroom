"""Poll inbox for new .eml files and refresh carrier status on every tracked row.

One pass = (1) ingest dropped emails, then (2) refresh carrier status for every
package in a non-terminal state. Priority per row: EasyPost first (retrieve the
existing tracker or create a new one), then fall back to carrier-specific
scraping (4PX API, vendor tracking page). Fires macOS notifications on
status transitions.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

from . import db, easypost, inbox, notify, scrape, settings

log = logging.getLogger(__name__)


# Carriers (especially FedEx) sometimes mark a package "delivered" hours before
# a failed-delivery / exception event lands, so we keep polling `delivered`
# rows for a grace window after the last carrier event. `received` is set by
# the user picking the package up and is always terminal.
DELIVERED_REPOLL_WINDOW = timedelta(hours=72)


def _parse_event_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _skip_pkg(pkg: dict) -> bool:
    status = pkg.get("status")
    if status not in easypost.TERMINAL_STATUSES:
        return False
    if status != "delivered":
        return True
    last = _parse_event_time(pkg.get("last_event_time"))
    if last is None:
        return True
    return datetime.now(timezone.utc) - last >= DELIVERED_REPOLL_WINDOW


def _interval_elapsed() -> bool:
    """True iff the user's configured gap has passed since the last successful poll.

    launchd fires this script on its own (plist) cadence. The user's preference
    is the minimum gap *between* polls, which we enforce here so changing it in
    the dashboard takes effect on the next tick without needing to reload
    launchd.
    """
    last = settings.get_last_poll_at()
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
    return elapsed >= settings.get_poll_interval_seconds()


def _try_easypost(pkg: dict) -> easypost.TrackerSnapshot | None:
    """Retrieve an existing tracker, or create one if we only have a tracking number.

    Returns a snapshot on success, or None on any EasyPost error (the error is
    persisted to `tracker_error` so the fallback path can still run).
    """
    row_id = pkg["id"]
    easypost_id = pkg.get("easypost_id")
    if easypost_id:
        try:
            return easypost.retrieve_tracker(easypost_id)
        except Exception as e:
            log.exception("EasyPost retrieve failed for row %s", row_id)
            db.set_tracker_error(row_id, f"{type(e).__name__}: {e}")
            return None

    tracking = pkg.get("tracking_number")
    if tracking:
        try:
            return easypost.create_tracker(tracking, carrier=pkg.get("carrier"))
        except Exception as e:
            log.exception("EasyPost create failed for row %s", row_id)
            db.set_tracker_error(row_id, f"{type(e).__name__}: {e}")
            return None

    return None


def poll_once(force: bool = False) -> dict[str, int]:
    db.init_schema()
    # Always ingest the inbox — watching for new .eml drops is cheap and the
    # user cares about new orders showing up promptly. The interval gate only
    # throttles the (chatty, rate-limited) carrier polling step below.
    inbox_summary = inbox.process_inbox()

    if not force and not _interval_elapsed():
        return {
            "inbox_seen": inbox_summary["seen"],
            "inbox_created": inbox_summary["created"],
            "inbox_updated": inbox_summary["updated"],
            "inbox_unrecognized": inbox_summary["unrecognized"],
            "inbox_failed": inbox_summary["failed"],
            "checked": 0,
            "updated": 0,
            "notified": 0,
            "errors": 0,
            "skipped": True,
        }

    packages = db.list_packages(include_delivered=True)
    carrier_summary = {"checked": 0, "updated": 0, "notified": 0, "errors": 0}

    for pkg in packages:
        if _skip_pkg(pkg):
            continue

        # Need at least one source of tracking info; otherwise nothing to do.
        if not (pkg.get("easypost_id") or pkg.get("tracking_number") or pkg.get("tracking_url")):
            continue

        carrier_summary["checked"] += 1
        old_status = pkg.get("status")
        description = pkg.get("description") or pkg.get("tracking_number") or ""

        # 1) Try EasyPost first — authoritative when it works.
        snap = _try_easypost(pkg)
        if snap is not None:
            db.update_status(
                row_id=pkg["id"],
                status=snap.status,
                est_delivery=snap.est_delivery,
                last_event=snap.last_event,
                last_event_time=snap.last_event_time,
                last_event_location=snap.last_event_location,
                events=snap.events,
                carrier=snap.carrier,
                easypost_id=snap.easypost_id,
            )
            if snap.status != old_status:
                carrier_summary["updated"] += 1
                notify.notify_status_change(
                    description=description,
                    old_status=old_status,
                    new_status=snap.status,
                    location=snap.last_event_location,
                    vendor=pkg.get("vendor"),
                    row_id=pkg["id"],
                )
                carrier_summary["notified"] += 1
            continue

        # 2) Fall back to scraping: carrier-specific API (e.g. 4PX) or URL scrape.
        scraped_ok = scrape.apply_to_row(
            pkg,
            old_status=old_status,
            description=description,
        )
        if scraped_ok:
            carrier_summary["updated"] += 1
        else:
            carrier_summary["errors"] += 1

    settings.record_poll_at()
    return {
        "inbox_seen": inbox_summary["seen"],
        "inbox_created": inbox_summary["created"],
        "inbox_updated": inbox_summary["updated"],
        "inbox_unrecognized": inbox_summary["unrecognized"],
        "inbox_failed": inbox_summary["failed"],
        **carrier_summary,
    }


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    try:
        summary = poll_once()
    except Exception:
        log.exception("poll failed")
        return 1
    log.info("poll complete: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
