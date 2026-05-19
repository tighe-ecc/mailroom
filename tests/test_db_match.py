"""Unit tests for db.find_match — order/PO/vendor matching across emails.

The MARK-10 / miniDSP duplicate bugs both stemmed from minor LLM extraction
variance on the order number across the order-confirmation and shipping-
confirmation emails for the same order. find_match needs to absorb that
variance.

Run: .venv/bin/python -m unittest tests.test_db_match -v
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mailroom import db


class FindMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self._tmp.close()
        self.db_path = Path(self._tmp.name)
        db.init_schema(self.db_path)

    def tearDown(self) -> None:
        self.db_path.unlink(missing_ok=True)

    def _add(self, **kwargs):
        kwargs.setdefault("description", "thing")
        return db.add_package(db_path=self.db_path, **kwargs)

    def test_norm_id_strips_separators_and_prefixes(self):
        self.assertEqual(db._norm_id("ORD-12345"), "12345")
        self.assertEqual(db._norm_id("Order #12345"), "12345")
        self.assertEqual(db._norm_id("ord_12345"), "12345")
        self.assertEqual(db._norm_id("00012345"), "12345")
        self.assertEqual(db._norm_id("INV/2026-001"), "2026001")
        self.assertEqual(db._norm_id("PO-ABC-123"), "ABC123")
        self.assertIsNone(db._norm_id(""))
        self.assertIsNone(db._norm_id(None))

    def test_norm_vendor_strips_corp_suffix(self):
        self.assertEqual(db._norm_vendor("MARK-10 Corporation"), db._norm_vendor("Mark-10 Corp"))
        self.assertEqual(db._norm_vendor("miniDSP Inc."), db._norm_vendor("MiniDSP"))

    def test_exact_order_number_match(self):
        rid = self._add(order_number="ORD-12345", vendor="MARK-10 Corporation")
        match = db.find_match(
            order_number="ORD-12345", vendor="MARK-10 Corporation", db_path=self.db_path
        )
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], rid)

    def test_normalized_order_number_match_across_email_variance(self):
        """LLM extracts 'ORD-12345' from the order email and '12345' from the
        shipping email. These should still match the same row."""
        rid = self._add(order_number="ORD-12345", vendor="MARK-10 Corporation")
        match = db.find_match(
            order_number="12345", vendor="MARK-10 Corporation", db_path=self.db_path
        )
        self.assertEqual(match["id"], rid)

    def test_cross_field_order_vs_po_match(self):
        """Some vendors call the same number a PO on one email and an order # on the other."""
        rid = self._add(po_number="12345", vendor="miniDSP")
        match = db.find_match(order_number="12345", vendor="miniDSP", db_path=self.db_path)
        self.assertEqual(match["id"], rid)

    def test_vendor_disambiguates_when_multiple_share_normalized_id(self):
        a = self._add(order_number="100", vendor="VendorA")
        b = self._add(order_number="100", vendor="VendorB")
        match = db.find_match(order_number="100", vendor="VendorB", db_path=self.db_path)
        self.assertEqual(match["id"], b)
        self.assertNotEqual(match["id"], a)

    def test_vendor_fallback_pairs_shipping_with_only_open_order(self):
        """Shipping confirmation has only tracking + vendor; if exactly one
        recent open order from that vendor exists, link to it instead of
        creating a new row."""
        rid = self._add(
            order_number="weird-internal-ref",
            vendor="MARK-10 Corporation",
            ordered_date="2026-04-10",
        )
        match = db.find_match(
            tracking_number="1Z999AA10123456784",
            vendor="MARK-10 Corporation",
            db_path=self.db_path,
        )
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], rid)

    def test_vendor_fallback_skips_when_multiple_open_orders(self):
        """Two open orders from same vendor → ambiguous; don't auto-link."""
        self._add(order_number="A", vendor="miniDSP")
        self._add(order_number="B", vendor="miniDSP")
        match = db.find_match(
            tracking_number="1Z999AA10123456784", vendor="miniDSP", db_path=self.db_path
        )
        self.assertIsNone(match)

    def test_vendor_fallback_skips_delivered_rows(self):
        rid = self._add(
            order_number="A", vendor="miniDSP", tracking_number="OLD123", status="delivered"
        )
        match = db.find_match(
            tracking_number="NEW456", vendor="miniDSP", db_path=self.db_path
        )
        self.assertIsNone(match, f"unexpectedly matched delivered row {rid}")

    def test_vendor_fallback_only_for_shipping_emails(self):
        """An order email (no tracking) shouldn't trigger vendor-only fallback —
        otherwise two distinct orders from the same vendor would collapse."""
        self._add(order_number="A", vendor="miniDSP")
        match = db.find_match(
            order_number="DIFFERENT", vendor="miniDSP", db_path=self.db_path
        )
        self.assertIsNone(match)

    def test_tracking_number_takes_priority(self):
        a = self._add(tracking_number="TRK-A", order_number="X", vendor="V")
        b = self._add(tracking_number="TRK-B", order_number="X", vendor="V")
        match = db.find_match(tracking_number="TRK-B", db_path=self.db_path)
        self.assertEqual(match["id"], b)

    def test_no_match_returns_none(self):
        self._add(order_number="A", vendor="VendorA")
        match = db.find_match(order_number="Z", vendor="VendorB", db_path=self.db_path)
        self.assertIsNone(match)

    # --- sender_domain matching ---

    def test_extract_sender_domain_handles_named_address(self):
        self.assertEqual(
            db.extract_sender_domain('"MARK-10 Corp" <orders@mark-10.com>'),
            "mark-10.com",
        )
        self.assertEqual(
            db.extract_sender_domain("orders@mark-10.com"), "mark-10.com"
        )
        self.assertIsNone(db.extract_sender_domain(""))
        self.assertIsNone(db.extract_sender_domain("plain text"))

    def test_norm_domain_strips_email_subdomain(self):
        self.assertEqual(db._norm_domain("mail.mark-10.com"), "mark-10.com")
        self.assertEqual(db._norm_domain("notifications.mark-10.com"), "mark-10.com")
        self.assertEqual(db._norm_domain("orders.minidsp.com"), "minidsp.com")
        # Don't strip the registrable domain itself.
        self.assertEqual(db._norm_domain("minidsp.com"), "minidsp.com")
        # www. is also stripped.
        self.assertEqual(db._norm_domain("www.mark-10.com"), "mark-10.com")

    def test_domain_disambiguates_when_vendor_strings_differ(self):
        """Order email From: orders@mark-10.com (vendor extracted as
        'MARK-10 Corporation'). Shipping email From: shipping@mark-10.com
        (vendor extracted as 'Mark-10'). Domain pairs them; vendor name alone
        wouldn't necessarily."""
        rid = self._add(
            order_number="ORD-12345",
            vendor="MARK-10 Corporation",
            sender_domain="mark-10.com",
        )
        match = db.find_match(
            order_number="12345",
            vendor="Mark-10",
            sender_domain="mark-10.com",
            db_path=self.db_path,
        )
        self.assertEqual(match["id"], rid)

    def test_domain_subdomain_normalization_matches_across_gateways(self):
        """orders.mark-10.com and notifications.mark-10.com both normalize to
        mark-10.com so they match the same row."""
        rid = self._add(
            order_number="100",
            vendor="MARK-10",
            sender_domain="orders.mark-10.com",
        )
        match = db.find_match(
            order_number="100",
            sender_domain="notifications.mark-10.com",
            db_path=self.db_path,
        )
        self.assertEqual(match["id"], rid)

    def test_domain_disambiguates_shared_order_number(self):
        """Two vendors happen to use order# '100'. Sender domain picks the right one."""
        a = self._add(order_number="100", vendor="VendorA", sender_domain="vendora.com")
        b = self._add(order_number="100", vendor="VendorB", sender_domain="vendorb.com")
        match = db.find_match(
            order_number="100", sender_domain="vendorb.com", db_path=self.db_path
        )
        self.assertEqual(match["id"], b)
        self.assertNotEqual(match["id"], a)

    def test_open_row_fallback_uses_domain(self):
        """Shipping confirmation has only tracking + domain (vendor extraction
        whiffed). Domain is enough to pair it with the only open order."""
        rid = self._add(
            order_number="weird-internal-ref",
            vendor="Some misspelled vendor name",
            sender_domain="mark-10.com",
        )
        match = db.find_match(
            tracking_number="1Z999AA10123456784",
            sender_domain="mail.mark-10.com",
            db_path=self.db_path,
        )
        self.assertEqual(match["id"], rid)

    # --- status regression guard ---

    def test_is_status_regression(self):
        self.assertTrue(db.is_status_regression("delivered", "confirmed"))
        self.assertTrue(db.is_status_regression("in_transit", "ordered"))
        self.assertTrue(db.is_status_regression("out_for_delivery", "in_transit"))
        # Forward moves are not regressions.
        self.assertFalse(db.is_status_regression("ordered", "confirmed"))
        self.assertFalse(db.is_status_regression("pre_transit", "delivered"))
        # Same-rank or identical: not a regression.
        self.assertFalse(db.is_status_regression("delivered", "delivered"))
        self.assertFalse(
            db.is_status_regression("out_for_delivery", "available_for_pickup")
        )
        # Missing values: not a regression (caller writes through).
        self.assertFalse(db.is_status_regression(None, "ordered"))
        self.assertFalse(db.is_status_regression("delivered", None))

    def test_open_row_fallback_skips_when_two_open_orders_same_domain(self):
        self._add(order_number="A", vendor="V", sender_domain="vendor.com")
        self._add(order_number="B", vendor="V", sender_domain="vendor.com")
        match = db.find_match(
            tracking_number="TRK", sender_domain="vendor.com", db_path=self.db_path
        )
        self.assertIsNone(match)

    def test_po_number_alone_doesnt_match_across_vendors(self):
        """Buyer-side PO ("FRD-249") is reused across every vendor on a project.
        A Protolabs email with PO FRD-249 must NOT land on the McMaster-Carr row
        that also has PO FRD-249 — otherwise the McMaster row gets overwritten
        with Protolabs data on every field. Regression test for the Protolabs
        parsing bug (feedback 2026-05-18).
        """
        mcmaster = self._add(
            order_number="64285979",
            po_number="FRD-249",
            vendor="McMaster-Carr",
            sender_domain="mcmaster.com",
        )
        match = db.find_match(
            order_number="6257-696",
            po_number="FRD-249",
            vendor="Protolabs",
            sender_domain="protolabs.com",
            db_path=self.db_path,
        )
        self.assertIsNone(
            match,
            f"Protolabs email wrongly matched McMaster row {mcmaster} via shared PO",
        )

    def test_po_number_still_matches_same_vendor(self):
        """The PO-number cross-vendor guard must not break the legitimate case
        where the order-confirmation and shipping-confirmation for the *same*
        vendor share the PO. Same vendor + same PO should still pair up."""
        rid = self._add(
            po_number="FRD-249",
            vendor="Protolabs",
            sender_domain="protolabs.com",
        )
        match = db.find_match(
            po_number="FRD-249",
            vendor="Protolabs",
            sender_domain="protolabs.com",
            db_path=self.db_path,
        )
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], rid)

    def test_po_number_matches_legacy_row_without_domain(self):
        """Manually-entered rows (no sender_domain, no vendor) still match by
        PO — there's nothing to disagree on, so the strict guard shouldn't
        reject them."""
        rid = self._add(po_number="FRD-249")  # legacy row: no vendor/domain
        match = db.find_match(
            po_number="FRD-249",
            vendor="Protolabs",
            sender_domain="protolabs.com",
            db_path=self.db_path,
        )
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], rid)


if __name__ == "__main__":
    unittest.main()
