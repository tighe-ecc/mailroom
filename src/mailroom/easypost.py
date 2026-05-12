"""Thin wrapper around the EasyPost SDK for creating and retrieving Trackers.

A Tracker is EasyPost's object representing a package you want status updates on.
Create one when adding a package; retrieve it on every poll to get the latest events.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from easypost import EasyPostClient


# Statuses for which we stop hitting the carrier API. "received" is the
# manually-set "I picked it up from the mailroom rack" state — once a row is
# there, polling is pointless.
TERMINAL_STATUSES = {
    "delivered", "received", "return_to_sender", "failure", "cancelled", "error",
}

# Carriers that require a linked carrier account by default (e.g. FedEx) expose a
# "<Name>Default" variant that uses EasyPost's shared account. We translate between
# the friendly name (what we store and show) and the API code (what EasyPost accepts
# and returns) so the UI never shows internal identifiers.
_CARRIER_TO_API = {
    "fedex": "FedexDefault",
}
_CARRIER_FROM_API = {
    "fedexdefault": "FedEx",
}


def _carrier_to_api(name: str | None) -> str | None:
    if not name:
        return name
    return _CARRIER_TO_API.get(name.strip().lower(), name)


def _carrier_from_api(name: str | None) -> str | None:
    if not name:
        return name
    return _CARRIER_FROM_API.get(name.strip().lower(), name)

ACTIVE_DISPLAY = {
    "ordered": "Ordered",
    "confirmed": "Confirmed",
    "in_fulfillment": "In fulfillment",
    "pre_transit": "Pre-transit",
    "in_transit": "In transit",
    "out_for_delivery": "Out for delivery",
    "available_for_pickup": "Ready for pickup",
}

TERMINAL_DISPLAY = {
    "delivered": "Delivered",
    "received": "Received",
    "return_to_sender": "Returned to sender",
    "failure": "Failure",
    "cancelled": "Cancelled",
    "error": "Error",
    "unknown": "Unknown",
}


@dataclass
class TrackerSnapshot:
    """Normalized view of an EasyPost Tracker."""

    easypost_id: str
    tracking_number: str
    carrier: str | None
    status: str
    est_delivery: str | None
    last_event: str | None
    last_event_time: str | None
    last_event_location: str | None
    events: list[dict[str, Any]]

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def _client() -> EasyPostClient:
    load_dotenv()
    key = os.environ.get("EASYPOST_API_KEY")
    if not key:
        raise RuntimeError(
            "EASYPOST_API_KEY not set. Copy .env.example to .env and add your key."
        )
    return EasyPostClient(key)


def _format_location(loc: Any) -> str | None:
    if not loc:
        return None
    city = getattr(loc, "city", None) or (loc.get("city") if isinstance(loc, dict) else None)
    state = getattr(loc, "state", None) or (loc.get("state") if isinstance(loc, dict) else None)
    country = getattr(loc, "country", None) or (loc.get("country") if isinstance(loc, dict) else None)
    parts = [p for p in (city, state, country) if p]
    return ", ".join(parts) if parts else None


def _event_to_dict(ev: Any) -> dict[str, Any]:
    return {
        "status": getattr(ev, "status", None),
        "message": getattr(ev, "message", None) or getattr(ev, "description", None),
        "datetime": getattr(ev, "datetime", None),
        "location": _format_location(getattr(ev, "tracking_location", None)),
    }


def _snapshot(tracker: Any) -> TrackerSnapshot:
    details = list(getattr(tracker, "tracking_details", []) or [])
    events = [_event_to_dict(ev) for ev in details]

    latest = details[-1] if details else None
    last_event = None
    last_event_time = None
    last_event_location = None
    if latest is not None:
        last_event = getattr(latest, "message", None) or getattr(latest, "description", None)
        last_event_time = getattr(latest, "datetime", None)
        last_event_location = _format_location(getattr(latest, "tracking_location", None))

    # EasyPost's top-level est_delivery_date is UTC; end-of-local-day commits (e.g.
    # FedEx 5PM Pacific) render as the next calendar day. carrier_detail exposes the
    # carrier's own local date, which matches the carrier's public tracking page.
    cd = getattr(tracker, "carrier_detail", None)
    local_eta = getattr(cd, "est_delivery_date_local", None) if cd is not None else None

    return TrackerSnapshot(
        easypost_id=tracker.id,
        tracking_number=tracker.tracking_code,
        carrier=_carrier_from_api(getattr(tracker, "carrier", None)),
        status=getattr(tracker, "status", "unknown") or "unknown",
        est_delivery=local_eta or getattr(tracker, "est_delivery_date", None),
        last_event=last_event,
        last_event_time=last_event_time,
        last_event_location=last_event_location,
        events=events,
    )


def create_tracker(tracking_number: str, carrier: str | None = None) -> TrackerSnapshot:
    """Register a new tracking number with EasyPost. Carrier is auto-detected if omitted."""
    payload: dict[str, Any] = {"tracking_code": tracking_number}
    api_carrier = _carrier_to_api(carrier)
    if api_carrier:
        payload["carrier"] = api_carrier
    tracker = _client().tracker.create(**payload)
    return _snapshot(tracker)


def retrieve_tracker(easypost_id: str) -> TrackerSnapshot:
    """Fetch the current state of a tracker by its EasyPost ID."""
    tracker = _client().tracker.retrieve(easypost_id)
    return _snapshot(tracker)


def display_status(status: str | None) -> str:
    if not status:
        return "Unknown"
    return ACTIVE_DISPLAY.get(status) or TERMINAL_DISPLAY.get(status) or status.replace("_", " ").title()
