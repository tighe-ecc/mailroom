"""Unit tests for the /packages/{id}/update route.

Covers the UNIQUE-constraint conflict path: typing a tracking number that
already belongs to a different row used to surface as a 500 IntegrityError
("Manual updates to the item cards do not save"); now it returns the detail
fragment with an inline error pointing at the drag-to-merge workflow.

Run: .venv/bin/python -m unittest tests.test_app_update -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class UpdateRowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self._tmp.close()
        os.environ["MAILROOM_DB"] = self._tmp.name

        # Import after env var is set so default_db_path() resolves to the temp DB.
        from mailroom import db

        self.db = db
        db.init_schema()

        # The /update route may try to call EasyPost when a tracking number is
        # added for the first time. Stub the network calls so tests stay
        # hermetic — we only care about the DB / response shape here.
        self._easypost_patch = patch("app.easypost.create_tracker")
        self._easypost_patch.start()

        # Skip watcher startup so we don't bind a real filesystem observer.
        self._lifespan_patch = patch("app.watcher.start", return_value=None)
        self._lifespan_patch.start()
        self._watcher_stop_patch = patch("app.watcher.stop", return_value=None)
        self._watcher_stop_patch.start()
        # Skip the on-startup re-ingest scan so the test doesn't reach into
        # the user's real ~/Mailroom/.mailroom/processed/ dir and burn LLM
        # credits parsing real .emls. Tracked so tearDown can stop it —
        # leaking this patcher poisons subsequent tests' inbox.reindex_all.
        self._reindex_patch = patch("app.inbox.reindex_all", return_value={})
        self._reindex_patch.start()

        import app as app_module

        self.client = TestClient(app_module.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self._easypost_patch.stop()
        self._lifespan_patch.stop()
        self._watcher_stop_patch.stop()
        self._reindex_patch.stop()
        Path(self._tmp.name).unlink(missing_ok=True)
        os.environ.pop("MAILROOM_DB", None)

    def test_update_tracking_conflict_returns_inline_error(self):
        """Typing a tracking # that already belongs to another row should not 500."""
        owner_id = self.db.add_package(
            tracking_number="DUP123", description="other shipment", vendor="OtherCo"
        )
        target_id = self.db.add_package(
            description="StepperOnline thing", vendor="StepperOnline"
        )

        resp = self.client.post(
            f"/packages/{target_id}/update",
            data={"tracking_number": "DUP123"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("Couldn't save", resp.text)
        self.assertIn("DUP123", resp.text)

        # The target row's tracking_number must remain unset — we refused the update.
        target = self.db.get_package(target_id)
        self.assertIsNone(target.get("tracking_number"))
        # The owning row is untouched.
        owner = self.db.get_package(owner_id)
        self.assertEqual(owner.get("tracking_number"), "DUP123")

    def test_update_same_tracking_number_is_a_noop_not_a_conflict(self):
        """Submitting the form without changing the tracking # must not self-conflict."""
        row_id = self.db.add_package(
            tracking_number="KEEP1", description="thing", vendor="V"
        )
        resp = self.client.post(
            f"/packages/{row_id}/update",
            data={"tracking_number": "KEEP1", "description": "renamed"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertNotIn("Couldn't save", resp.text)
        row = self.db.get_package(row_id)
        self.assertEqual(row.get("tracking_number"), "KEEP1")
        self.assertEqual(row.get("description"), "renamed")


if __name__ == "__main__":
    unittest.main()
