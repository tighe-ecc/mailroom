"""Unit tests for the dashboard "Received" checkbox workflow.

Companion to the data-model PR: this PR wires the UI to flip a row from
delivered → received, and changes the default-view filter so delivered
rows stay visible (the user needs to see what's awaiting pickup) but
received rows drop out (the user is done with them).

Run: .venv/bin/python -m unittest tests.test_received_checkbox -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class ReceiveEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self._tmp.close()
        os.environ["MAILROOM_DB"] = self._tmp.name

        from mailroom import db

        self.db = db
        db.init_schema()

        # Avoid hitting the network / filesystem during tests.
        self._easypost_patch = patch("app.easypost.create_tracker")
        self._easypost_patch.start()
        self._lifespan_patch = patch("app.watcher.start", return_value=None)
        self._lifespan_patch.start()
        self._watcher_stop_patch = patch("app.watcher.stop", return_value=None)
        self._watcher_stop_patch.start()
        # Skip the on-startup re-ingest scan so the test doesn't reach into
        # the user's real ~/Mailroom/.mailroom/processed/ dir. Tracked in
        # tearDown — a leaked patcher poisons inbox.reindex_all in any test
        # that runs afterwards.
        self._reindex_patch = patch("app.inbox.reindex_all", return_value={})
        self._reindex_patch.start()
        # MAILROOM_QUIET silences notify.send() so an inadvertent ingest can't
        # spam the user's Notification Center during a test run.
        self._old_quiet = os.environ.get("MAILROOM_QUIET")
        os.environ["MAILROOM_QUIET"] = "1"

        import app as app_module

        self.client = TestClient(app_module.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self._easypost_patch.stop()
        self._lifespan_patch.stop()
        self._watcher_stop_patch.stop()
        self._reindex_patch.stop()
        if self._old_quiet is None:
            os.environ.pop("MAILROOM_QUIET", None)
        else:
            os.environ["MAILROOM_QUIET"] = self._old_quiet
        Path(self._tmp.name).unlink(missing_ok=True)
        os.environ.pop("MAILROOM_DB", None)

    def test_receive_flips_delivered_to_received(self):
        row_id = self.db.add_package(
            description="endmills", status="delivered", tracking_number="ABC"
        )
        resp = self.client.post(f"/packages/{row_id}/receive")
        self.assertEqual(resp.status_code, 200, resp.text)
        row = self.db.get_package(row_id)
        self.assertEqual(row["status"], "received")

    def test_receive_404_for_missing_row(self):
        resp = self.client.post("/packages/999999/receive")
        self.assertEqual(resp.status_code, 404)

    def test_default_view_shows_delivered_hides_received(self):
        """Behavior change in this PR: delivered rows must stay visible by
        default so the user can see what's awaiting pickup; received rows
        drop out (they're done)."""
        delivered_id = self.db.add_package(description="d", status="delivered")
        received_id = self.db.add_package(description="r", status="received")

        rows = self.db.list_packages()
        ids = {r["id"] for r in rows}
        self.assertIn(delivered_id, ids, "delivered must remain visible by default")
        self.assertNotIn(received_id, ids, "received must drop out by default")

        # With the toggle on, both come back.
        rows_all = self.db.list_packages(include_delivered=True)
        ids_all = {r["id"] for r in rows_all}
        self.assertIn(delivered_id, ids_all)
        self.assertIn(received_id, ids_all)

    def test_received_checkbox_renders_only_for_delivered_rows(self):
        delivered_id = self.db.add_package(description="d", status="delivered")
        in_transit_id = self.db.add_package(description="t", status="in_transit")
        resp = self.client.get("/packages")
        self.assertEqual(resp.status_code, 200)
        # Checkbox should target the delivered row's receive endpoint
        self.assertIn(f'/packages/{delivered_id}/receive', resp.text)
        # No checkbox for the in-transit row
        self.assertNotIn(f'/packages/{in_transit_id}/receive', resp.text)


if __name__ == "__main__":
    unittest.main()
