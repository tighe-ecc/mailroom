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


def _parsed(
    kind: str,
    ordered_date: str | None = None,
    ordered_date_confidence: float = 0.5,
) -> parser.ParsedEmail:
    return parser.ParsedEmail(
        kind=kind,
        vendor="StepperOnline",
        order_number="300609",
        po_number=None,
        item_description="Stepper motor",
        ordered_date=ordered_date,
        ordered_date_confidence=ordered_date_confidence,
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
    def test_order_confirmation_low_confidence_uses_email_date(self):
        """The historical bug: LLM extracts a body date with ~0.5 confidence
        (quote-validity / promised-ship). With the new gate, the email Date
        header wins instead."""
        parsed = _parsed(
            "order_confirmation", ordered_date="2026-07-05", ordered_date_confidence=0.5
        )
        self.assertEqual(
            inbox._resolve_ordered_date(parsed, "2026-05-06"), "2026-05-06"
        )

    def test_order_confirmation_high_confidence_uses_body_date(self):
        """When the LLM is near-certain the body explicitly labels the date
        as the order date, trust the body — the email may have been sent a
        day or two after the actual order was placed."""
        parsed = _parsed(
            "order_confirmation", ordered_date="2026-04-10", ordered_date_confidence=0.95
        )
        self.assertEqual(
            inbox._resolve_ordered_date(parsed, "2026-04-12"), "2026-04-10"
        )

    def test_order_confirmation_falls_back_to_parsed_when_no_header_date(self):
        """No email Date header → use whatever the LLM had, regardless of confidence."""
        parsed = _parsed(
            "order_confirmation", ordered_date="2026-04-10", ordered_date_confidence=0.3
        )
        self.assertEqual(inbox._resolve_ordered_date(parsed, None), "2026-04-10")

    def test_shipping_confirmation_keeps_parsed_date(self):
        """Shipping confirmations are sent AFTER the order — the Date header
        is the ship date, not the order date. Don't overwrite the LLM-extracted
        ordered_date if it has one. Confidence doesn't apply here."""
        parsed = _parsed(
            "shipping_confirmation", ordered_date="2026-04-10", ordered_date_confidence=0.2
        )
        self.assertEqual(
            inbox._resolve_ordered_date(parsed, "2026-04-15"), "2026-04-10"
        )

    def test_high_confidence_far_future_body_date_rejected(self):
        """The DD/MM-vs-MM/DD misread case: StepperOnline "Date Added: 07/05/2026"
        is May 7 (DD/MM), but the LLM confidently parses it as July 5 (MM/DD)
        with 0.95 confidence. Without the date-sanity gate, the bad date wins;
        with it, the email Date header wins because Jul 5 sits 60 days in the
        future of the May 6 email."""
        parsed = _parsed(
            "order_confirmation", ordered_date="2026-07-05", ordered_date_confidence=0.95
        )
        self.assertEqual(
            inbox._resolve_ordered_date(parsed, "2026-05-06"), "2026-05-06"
        )

    def test_high_confidence_far_past_body_date_rejected(self):
        """A confident extraction months in the past is also implausible — the
        StepperOnline 299907 case where the LLM had picked '2026-02-05' from an
        email actually sent on 2026-05-02."""
        parsed = _parsed(
            "order_confirmation", ordered_date="2026-02-05", ordered_date_confidence=0.95
        )
        self.assertEqual(
            inbox._resolve_ordered_date(parsed, "2026-05-02"), "2026-05-02"
        )

    def test_high_confidence_one_day_back_accepted(self):
        """The common, legitimate case: the email Date header is one day after
        the actual order placement (vendor's nightly batch send). The body
        extraction is the more accurate date and must still win."""
        parsed = _parsed(
            "order_confirmation", ordered_date="2026-04-27", ordered_date_confidence=0.95
        )
        self.assertEqual(
            inbox._resolve_ordered_date(parsed, "2026-04-28"), "2026-04-27"
        )

    def test_high_confidence_no_header_skips_window_check(self):
        """No email_date to anchor against → trust the body extraction. We have
        no way to evaluate plausibility, and rejecting all body dates here would
        leave ordered_date null."""
        parsed = _parsed(
            "order_confirmation", ordered_date="2026-07-05", ordered_date_confidence=0.95
        )
        self.assertEqual(inbox._resolve_ordered_date(parsed, None), "2026-07-05")

    def test_shipping_confirmation_with_no_parsed_date_stays_none(self):
        parsed = _parsed("shipping_confirmation", ordered_date=None)
        self.assertIsNone(inbox._resolve_ordered_date(parsed, "2026-04-15"))


if __name__ == "__main__":
    unittest.main()
