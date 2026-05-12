"""Unit tests for ordered-date resolution from email Date headers.

Covers the StepperOnline bug: the LLM was extracting a "Sun Jul 5" date out
of the email body (a quote-validity / promised-ship date) and using it as
ordered_date. The Date header is now the source of truth for
order_confirmation emails.

Run: .venv/bin/python -m unittest tests.test_inbox_date -v
"""

from __future__ import annotations

import unittest

from mailroom import inbox, parser


def _parsed(kind: str, ordered_date: str | None = None) -> parser.ParsedEmail:
    return parser.ParsedEmail(
        kind=kind,
        vendor="StepperOnline",
        order_number="300609",
        po_number=None,
        item_description="Stepper motor",
        ordered_date=ordered_date,
        promised_ship_date=None,
        promised_delivery_date=None,
        lead_time_days=None,
        tracking_number=None,
        carrier=None,
        tracking_url=None,
        status_signal="confirmed",
        confidence=0.9,
        notes=None,
    )


class HeaderDateTests(unittest.TestCase):
    def test_rfc2822_header_parses_to_isodate(self):
        self.assertEqual(
            inbox._header_date("Wed, 06 May 2026 17:00:23 -0700"), "2026-05-06"
        )

    def test_missing_header_returns_none(self):
        self.assertIsNone(inbox._header_date(None))
        self.assertIsNone(inbox._header_date(""))

    def test_malformed_header_returns_none(self):
        self.assertIsNone(inbox._header_date("not a real date"))


class ResolveOrderedDateTests(unittest.TestCase):
    def test_order_confirmation_prefers_email_date(self):
        """Even when the LLM has an ordered_date, the email Date wins for
        order_confirmation emails — the body's date is unreliable (it might
        be a quote-validity or promised-ship date, not the order date)."""
        parsed = _parsed("order_confirmation", ordered_date="2026-07-05")
        self.assertEqual(
            inbox._resolve_ordered_date(parsed, "2026-05-06"), "2026-05-06"
        )

    def test_order_confirmation_falls_back_to_parsed_when_no_header_date(self):
        parsed = _parsed("order_confirmation", ordered_date="2026-04-10")
        self.assertEqual(inbox._resolve_ordered_date(parsed, None), "2026-04-10")

    def test_shipping_confirmation_keeps_parsed_date(self):
        """Shipping confirmations are sent AFTER the order — the Date header
        is the ship date, not the order date. Don't overwrite the LLM-extracted
        ordered_date if it has one."""
        parsed = _parsed("shipping_confirmation", ordered_date="2026-04-10")
        self.assertEqual(
            inbox._resolve_ordered_date(parsed, "2026-04-15"), "2026-04-10"
        )

    def test_shipping_confirmation_with_no_parsed_date_stays_none(self):
        parsed = _parsed("shipping_confirmation", ordered_date=None)
        self.assertIsNone(inbox._resolve_ordered_date(parsed, "2026-04-15"))


if __name__ == "__main__":
    unittest.main()
