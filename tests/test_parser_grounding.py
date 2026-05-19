"""Unit tests for the parser's grounding pass.

Covers the feedback that LLM extraction can cross-contaminate fields between
similar templates (e.g. a Protolabs FedEx shipment ends up with carrier=UPS
and a UPS tracking URL pointing at a totally different shipment's tracking
number). Grounding drops any field whose value isn't substring-present in the
source text — vendor-agnostic, no per-vendor branches.

Run: .venv/bin/python -m unittest tests.test_parser_grounding -v
"""

from __future__ import annotations

import unittest

from mailroom import parser


def _raw(**overrides):
    base = {
        "kind": "shipping_confirmation",
        "vendor": "Protolabs",
        "order_number": "6257-696",
        "po_number": "FRD-249",
        "item_description": "Two SLDPRT parts",
        "ordered_date": None,
        "ordered_date_confidence": 0.0,
        "promised_ship_date": None,
        "promised_delivery_date": None,
        "lead_time_days": None,
        "tracking_number": "381386875690",
        "carrier": "FedEx",
        "tracking_url": "https://www.fedex.com/fedextrack/?trknbr=381386875690",
        "status_signal": "shipped",
        "confidence": 0.95,
        "notes": None,
    }
    base.update(overrides)
    return base


GROUNDED_BODY = (
    "Part(s) shipped! Your part(s) for order number 6257-696 are on their way.\n"
    "Tracking number: 381386875690\n"
    "Ship date: 2026-05-18\n"
    "PO: FRD-249\n"
    "Shipping Method: FedEx\n"
)


class GroundingTests(unittest.TestCase):
    def test_clean_extraction_passes_through(self):
        p = parser._coerce(_raw())
        grounded = parser._ground(p, body=GROUNDED_BODY, subject="", sender="")
        self.assertEqual(grounded.tracking_number, "381386875690")
        self.assertEqual(grounded.carrier, "FedEx")
        self.assertEqual(
            grounded.tracking_url,
            "https://www.fedex.com/fedextrack/?trknbr=381386875690",
        )

    def test_hallucinated_carrier_dropped(self):
        # Body says FedEx, model returns UPS. Carrier must be dropped.
        p = parser._coerce(_raw(carrier="UPS"))
        grounded = parser._ground(p, body=GROUNDED_BODY, subject="", sender="")
        self.assertIsNone(grounded.carrier)
        # Real tracking number still survives.
        self.assertEqual(grounded.tracking_number, "381386875690")

    def test_cross_contaminated_tracking_url_dropped(self):
        # Real tracking_number is 381…690 but the URL carries a different
        # shipment's UPS number that isn't in the body. URL must be dropped.
        p = parser._coerce(_raw(
            carrier="UPS",  # also hallucinated; gets dropped
            tracking_url=(
                "https://www.ups.com/track?loc=en_US"
                "&tracknum=1Z90W3240262341201&AgreeToTermsAndConditions=yes"
            ),
        ))
        grounded = parser._ground(p, body=GROUNDED_BODY, subject="", sender="")
        self.assertIsNone(grounded.tracking_url)
        self.assertIsNone(grounded.carrier)
        # Verified tracking number stays.
        self.assertEqual(grounded.tracking_number, "381386875690")

    def test_hallucinated_tracking_number_dropped(self):
        p = parser._coerce(_raw(tracking_number="9999999999999999"))
        grounded = parser._ground(p, body=GROUNDED_BODY, subject="", sender="")
        self.assertIsNone(grounded.tracking_number)

    def test_grounded_url_with_real_tracking_number_survives(self):
        body_with_url = GROUNDED_BODY + (
            "Track at https://www.fedex.com/fedextrack/?trknbr=381386875690\n"
        )
        p = parser._coerce(_raw())
        grounded = parser._ground(p, body=body_with_url, subject="", sender="")
        self.assertEqual(
            grounded.tracking_url,
            "https://www.fedex.com/fedextrack/?trknbr=381386875690",
        )

    def test_grounding_tolerates_html_artifacts(self):
        # The .eml-rendered body sometimes runs labels and values together or
        # adds whitespace. Normalized substring match (case-insensitive, drop
        # non-alnum) should still find values.
        body = (
            "<p>Tracking #</p><p>3 8 1 3 8 6 8 7 5 6 9 0</p>"
            "<p>Shipping Method: fedex</p>"
        )
        p = parser._coerce(_raw())
        grounded = parser._ground(p, body=body, subject="", sender="")
        self.assertEqual(grounded.tracking_number, "381386875690")
        self.assertEqual(grounded.carrier, "FedEx")

    def test_hallucinated_order_and_po_dropped(self):
        body = (
            "Tracking number: 381386875690\n"
            "Shipping Method: FedEx\n"
        )
        # Real order/po not present in body; both should drop.
        p = parser._coerce(_raw(order_number="6257-696", po_number="FRD-249"))
        grounded = parser._ground(p, body=body, subject="", sender="")
        self.assertIsNone(grounded.order_number)
        self.assertIsNone(grounded.po_number)

    def test_subject_and_sender_count_as_source(self):
        # Order number often only appears in the subject. The grounding
        # check pools subject + sender + body so a real-in-subject value
        # is still considered grounded.
        body = "Tracking number: 381386875690\nShipping Method: FedEx\n"
        subject = "Protolabs shipping confirmation: Order 6257-696"
        sender = "Protolabs <customerservice@protolabs.com>"
        p = parser._coerce(_raw(po_number=None))  # po not in this email
        grounded = parser._ground(p, body=body, subject=subject, sender=sender)
        self.assertEqual(grounded.order_number, "6257-696")


class UrlTokenExtractionTests(unittest.TestCase):
    def test_query_string_values_extracted(self):
        tokens = parser._extract_url_tokens(
            "https://www.ups.com/track?loc=en_US&tracknum=1Z90W3240262341201"
        )
        self.assertIn("1Z90W3240262341201", tokens)

    def test_short_values_ignored(self):
        tokens = parser._extract_url_tokens("https://x.com/?a=1&b=yes")
        self.assertEqual(tokens, [])

    def test_path_segments_extracted(self):
        tokens = parser._extract_url_tokens(
            "https://track.4px.com/result/4PX12345ABCDE/en-us"
        )
        # Path segments of length>=6 captured.
        self.assertIn("4PX12345ABCDE", tokens)


class NormalizeTests(unittest.TestCase):
    def test_normalize_strips_punctuation(self):
        self.assertEqual(parser._normalize("FedEx Express"), "fedexexpress")

    def test_normalize_lowercases(self):
        self.assertEqual(parser._normalize("UPS"), "ups")
        self.assertEqual(parser._normalize("Federal Express"), "federalexpress")

    def test_appears_in_substring_match(self):
        norm = parser._normalize("Shipping Method: FedEx\nTracking: 123-456-789-0")
        self.assertTrue(parser._appears_in("fedex", norm))
        self.assertTrue(parser._appears_in("1234567890", norm))
        self.assertFalse(parser._appears_in("ups", norm))


if __name__ == "__main__":
    unittest.main()
