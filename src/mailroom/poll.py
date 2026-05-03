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

from . import db, easypost, inbox, notify, scrape

log = logging.getLogger(__name__)


def _skip_status(status: str | None) -> bool:
    return status in easypost.TERMINAL_STATUSES


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


def poll_once() -> dict[str, int]:
    db.init_schema()
    inbox_summary = inbox.process_inbox()

    packages = db.list_packages(include_delivered=True)
    carrier_summary = {"checked": 0, "updated": 0, "notified": 0, "errors": 0}

    for pkg in packages:
        if _skip_status(pkg.get("status")):
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
