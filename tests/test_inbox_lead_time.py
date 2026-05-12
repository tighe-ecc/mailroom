"""Unit tests for lead-time → estimated-delivery resolution.

Covers the feedback: emails that quote "6-8 weeks" (no absolute delivery
date) should still populate ETA in the dashboard by anchoring the upper
bound of the lead time to the order date.

Run: .venv/bin/python -m unittest tests.test_inbox_lead_time -v
"""

from __future__ import annotations

import unittest

from mailroom import inbox, parser


def _parsed(
    ordered_date: str | None = "2026-05-06",
    promised_delivery_date: str | None = None,
    lead_time_days: int | None = None,
) -> parser.ParsedEmail:
    return parser.ParsedEmail(
        kind="order_confirmation",
        vendor="V",
        order_number="1",
        po_number=None,
        item_description="x",
        ordered_date=ordered_date,
        promised_ship_date=None,
        promised_delivery_date=promised_delivery_date,
        lead_time_days=lead_time_days,
        tracking_number=None,
        carrier=None,
        tracking_url=None,
        status_signal="confirmed",
        confidence=0.9,
        notes=None,
    )


class ResolveDeliveryEstimateTests(unittest.TestCase):
    def test_absolute_date_passes_through(self):
        p = _parsed(promised_delivery_date="2026-05-20", lead_time_days=14)
        self.assertEqual(inbox._resolve_delivery_estimate(p), "2026-05-20")

    def test_lead_time_anchored_to_order_date(self):
        p = _parsed(ordered_date="2026-05-06", lead_time_days=56)  # 6-8 weeks => 56
        self.assertEqual(inbox._resolve_delivery_estimate(p), "2026-07-01")

    def test_short_business_lead_time(self):
        # "ships in 3 business days" → LLM normalizes to ~5 calendar days
        p = _parsed(ordered_date="2026-05-06", lead_time_days=5)
        self.assertEqual(inbox._resolve_delivery_estimate(p), "2026-05-11")

    def test_no_order_date_means_no_estimate(self):
        # Without an anchor we have nothing to add the lead time to.
        p = _parsed(ordered_date=None, lead_time_days=56)
        self.assertIsNone(inbox._resolve_delivery_estimate(p))

    def test_no_lead_time_and_no_promised_returns_none(self):
        p = _parsed(ordered_date="2026-05-06")
        self.assertIsNone(inbox._resolve_delivery_estimate(p))


class ParserLeadTimeCoercionTests(unittest.TestCase):
    def test_lead_time_pulled_from_payload(self):
        p = parser._coerce({"kind": "order_confirmation", "lead_time_days": 56})
        self.assertEqual(p.lead_time_days, 56)

    def test_lead_time_missing_is_none(self):
        p = parser._coerce({"kind": "order_confirmation"})
        self.assertIsNone(p.lead_time_days)

    def test_lead_time_garbage_is_dropped(self):
        # LLM occasionally hallucinates strings; coerce defensively.
        p = parser._coerce({"kind": "order_confirmation", "lead_time_days": "soon"})
        self.assertIsNone(p.lead_time_days)

    def test_lead_time_zero_or_negative_dropped(self):
        self.assertIsNone(parser._coerce({"kind": "order_confirmation", "lead_time_days": 0}).lead_time_days)
        self.assertIsNone(parser._coerce({"kind": "order_confirmation", "lead_time_days": -3}).lead_time_days)

    def test_lead_time_implausibly_large_dropped(self):
        # Sanity bound — a year-plus suggests the LLM misread something.
        self.assertIsNone(
            parser._coerce({"kind": "order_confirmation", "lead_time_days": 999}).lead_time_days
        )


if __name__ == "__main__":
    unittest.main()
