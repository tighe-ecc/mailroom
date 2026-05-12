"""Unit tests for the new "received" status.

Models the "Received" final state — the user has physically picked the
package up from the mailroom rack. Sits one rank above "delivered" so
delivered → received is forward progress, never a regression, and is in
TERMINAL_STATUSES so carrier polling halts.

Run: .venv/bin/python -m unittest tests.test_received_status -v
"""

from __future__ import annotations

import unittest

from mailroom import db, easypost


class ReceivedStatusModelTests(unittest.TestCase):
    def test_received_ranks_above_delivered(self):
        self.assertGreater(db.STATUS_RANK["received"], db.STATUS_RANK["delivered"])

    def test_delivered_to_received_is_not_a_regression(self):
        """User clicking the to-be-built "Received" checkbox on a Delivered
        row is forward progress, never a regression."""
        self.assertFalse(db.is_status_regression("delivered", "received"))

    def test_received_to_anything_earlier_is_a_regression(self):
        self.assertTrue(db.is_status_regression("received", "delivered"))
        self.assertTrue(db.is_status_regression("received", "in_transit"))
        self.assertTrue(db.is_status_regression("received", "out_for_delivery"))

    def test_received_in_terminal_sets(self):
        # Both halt-polling (easypost) and hide-from-default-view (db) treat
        # received as terminal — the row is done.
        self.assertIn("received", db.TERMINAL_STATUSES)
        self.assertIn("received", easypost.TERMINAL_STATUSES)

    def test_display_label(self):
        self.assertEqual(easypost.display_status("received"), "Received")

    def test_list_packages_hides_received_by_default(self):
        import os
        import tempfile
        from pathlib import Path

        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        tmp.close()
        try:
            db.init_schema(Path(tmp.name))
            active_id = db.add_package(
                description="active", status="in_transit", db_path=Path(tmp.name)
            )
            received_id = db.add_package(
                description="picked up", status="received", db_path=Path(tmp.name)
            )

            ids_default = {r["id"] for r in db.list_packages(db_path=Path(tmp.name))}
            self.assertIn(active_id, ids_default)
            self.assertNotIn(received_id, ids_default)

            ids_all = {
                r["id"] for r in db.list_packages(
                    include_delivered=True, db_path=Path(tmp.name)
                )
            }
            self.assertIn(received_id, ids_all)
        finally:
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
