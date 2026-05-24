"""Recently-`delivered` packages must stay in the poll set so a later
delivery-attempted / exception event from the carrier can correct the record.

Background: a user reported a FedEx package whose dashboard row showed
`delivered` while the carrier site showed a failed delivery attempt. EasyPost
had marked the package delivered prematurely, and our poller had stopped
asking because `delivered` was treated as fully terminal. We now keep polling
delivered rows for a grace window after the last carrier event; the
user-set `received` state remains fully terminal regardless of time.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from mailroom import poll


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class SkipPkgTests(unittest.TestCase):
    def test_active_status_is_polled(self):
        self.assertFalse(poll._skip_pkg({"status": "in_transit"}))

    def test_received_is_terminal_regardless_of_time(self):
        # User-set: once they've picked it up, polling is pointless even
        # if the last carrier event was 5 minutes ago.
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        self.assertTrue(poll._skip_pkg({
            "status": "received",
            "last_event_time": _iso(recent),
        }))

    def test_recently_delivered_is_repolled(self):
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        self.assertFalse(poll._skip_pkg({
            "status": "delivered",
            "last_event_time": _iso(recent),
        }))

    def test_long_delivered_is_skipped(self):
        old = datetime.now(timezone.utc) - poll.DELIVERED_REPOLL_WINDOW - timedelta(hours=1)
        self.assertTrue(poll._skip_pkg({
            "status": "delivered",
            "last_event_time": _iso(old),
        }))

    def test_delivered_without_event_time_is_skipped(self):
        # No timestamp means we can't tell how stale it is — fall back to the
        # old behavior of treating delivered as terminal so we don't churn.
        self.assertTrue(poll._skip_pkg({"status": "delivered"}))
        self.assertTrue(poll._skip_pkg({"status": "delivered", "last_event_time": None}))

    def test_other_terminal_statuses_still_skipped(self):
        # return_to_sender / failure / cancelled / error are carrier-final;
        # only `delivered` gets the grace window.
        for status in ("return_to_sender", "failure", "cancelled", "error"):
            recent = datetime.now(timezone.utc) - timedelta(minutes=5)
            with self.subTest(status=status):
                self.assertTrue(poll._skip_pkg({
                    "status": status,
                    "last_event_time": _iso(recent),
                }))


if __name__ == "__main__":
    unittest.main()
