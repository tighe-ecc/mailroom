"""Unit tests for db.merge_packages — the drag-to-combine dashboard feature.

Run: .venv/bin/python -m unittest tests.test_db_merge -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mailroom import db


class MergePackagesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self._tmp.close()
        self.db_path = Path(self._tmp.name)
        db.init_schema(self.db_path)

    def tearDown(self) -> None:
        self.db_path.unlink(missing_ok=True)
        log = db.merge_log_path(self.db_path)
        log.unlink(missing_ok=True)

    def _add(self, **kwargs):
        kwargs.setdefault("description", "thing")
        return db.add_package(db_path=self.db_path, **kwargs)

    def test_destination_keeps_filled_fields_source_fills_gaps(self):
        # User scenario: drag the shipping-confirmation row (src) onto the
        # order-confirmation row (dst). dst keeps everything it has; src fills
        # the tracking/carrier gaps it brought in.
        dst_id = self._add(
            description="Mark-10 force gauge",
            vendor="Mark-10",
            order_number="ORD-12345",
            sender_domain="mark-10.com",
            ordered_date="2026-04-01",
            status="ordered",
        )
        src_id = self._add(
            description="Force gauge - 100 lbf",
            vendor="MARK-10 Corp",
            tracking_number="1Z999AA10123456784",
            carrier="UPS",
            status="in_transit",
        )

        src_before, dst_before, merged = db.merge_packages(
            src_id, dst_id, db_path=self.db_path
        )

        self.assertEqual(src_before["id"], src_id)
        self.assertEqual(dst_before["id"], dst_id)
        # dst kept its description/vendor/order_number/status — it's already
        # filled, so src doesn't overwrite. Drag direction = user choice.
        self.assertEqual(merged["description"], "Mark-10 force gauge")
        self.assertEqual(merged["vendor"], "Mark-10")
        self.assertEqual(merged["order_number"], "ORD-12345")
        self.assertEqual(merged["status"], "ordered")
        # src filled the gaps
        self.assertEqual(merged["tracking_number"], "1Z999AA10123456784")
        self.assertEqual(merged["carrier"], "UPS")
        # source row is gone
        self.assertIsNone(db.get_package(src_id, db_path=self.db_path))
        # destination row reflects the merge
        live = db.get_package(dst_id, db_path=self.db_path)
        assert live is not None
        self.assertEqual(live["tracking_number"], "1Z999AA10123456784")
        self.assertEqual(live["order_number"], "ORD-12345")
        self.assertEqual(live["vendor"], "Mark-10")

    def test_tracking_number_unique_collision_resolved_by_delete_first(self):
        # Both rows have tracking_numbers; dst wins, src is dropped.
        dst_id = self._add(tracking_number="DST-TRACK", description="dst")
        src_id = self._add(tracking_number="SRC-TRACK", description="src")
        _, _, merged = db.merge_packages(src_id, dst_id, db_path=self.db_path)
        self.assertEqual(merged["tracking_number"], "DST-TRACK")
        self.assertIsNone(db.get_package(src_id, db_path=self.db_path))

    def test_swapping_tracking_when_dst_has_none(self):
        # dst has no tracking; src does — merged row inherits the tracking
        # without violating the UNIQUE constraint (src is deleted first).
        dst_id = self._add(description="dst", order_number="A1")
        src_id = self._add(description="src", tracking_number="ABC123")
        _, _, merged = db.merge_packages(src_id, dst_id, db_path=self.db_path)
        self.assertEqual(merged["tracking_number"], "ABC123")
        self.assertEqual(merged["order_number"], "A1")

    def test_self_merge_rejected(self):
        row_id = self._add(description="x")
        with self.assertRaises(ValueError):
            db.merge_packages(row_id, row_id, db_path=self.db_path)

    def test_missing_row_rejected(self):
        row_id = self._add(description="x")
        with self.assertRaises(ValueError):
            db.merge_packages(row_id, 999_999, db_path=self.db_path)
        with self.assertRaises(ValueError):
            db.merge_packages(999_999, row_id, db_path=self.db_path)

    def test_log_merge_writes_jsonl_record(self):
        dst_id = self._add(description="dst", order_number="A1")
        src_id = self._add(description="src", tracking_number="ABC123")
        src_before, dst_before, merged = db.merge_packages(
            src_id, dst_id, db_path=self.db_path
        )
        db.log_merge(src_before, dst_before, merged, db_path=self.db_path)

        log_path = db.merge_log_path(self.db_path)
        self.assertTrue(log_path.exists())
        lines = log_path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertIn("timestamp", record)
        self.assertEqual(record["src"]["tracking_number"], "ABC123")
        self.assertEqual(record["dst"]["order_number"], "A1")
        self.assertEqual(record["merged"]["tracking_number"], "ABC123")
        self.assertEqual(record["merged"]["order_number"], "A1")


if __name__ == "__main__":
    unittest.main()
