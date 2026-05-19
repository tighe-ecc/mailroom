"""Pin the rule: an existing row's vendor / sender_domain survives a
carrier-side notification email.

Background: today row 27 (a Futek order) ingested correctly first as
"FUTEK Advanced Sensor Technology, Inc." from a vendor-side order
confirmation. Then a subsequent "UPS Delivery Notification" email
came in, was correctly merged onto the same row by tracking_number,
but its parsed `vendor="UPS"` / `sender_domain="ups.com"` overwrote
the Futek identity. The row's tracking state was right; the dashboard
just couldn't find it under "Futek" anymore.

_apply now detects carrier-side notifications (parsed.vendor ==
parsed.carrier, OR sender_domain is a known carrier domain) and
preserves the existing row's vendor + sender_domain.

Run: .venv/bin/python -m unittest tests.test_carrier_notification_preserves_vendor -v
"""

from __future__ import annotations

import unittest
from mailroom import inbox, parser


def _make(**overrides) -> parser.ParsedEmail:
    base = {
        "kind": "shipping_confirmation",
        "vendor": None,
        "order_number": None,
        "po_number": None,
        "item_description": None,
        "ordered_date": None,
        "ordered_date_confidence": 0.0,
        "promised_ship_date": None,
        "promised_delivery_date": None,
        "lead_time_days": None,
        "tracking_number": None,
        "carrier": None,
        "tracking_url": None,
        "status_signal": None,
        "confidence": 0.9,
        "notes": None,
    }
    base.update(overrides)
    return parser.ParsedEmail(**base)


class IsCarrierNotificationTests(unittest.TestCase):
    def test_vendor_equals_carrier_is_a_carrier_email(self) -> None:
        # The "vendor field matches the carrier field" signal — purely
        # structural, vendor-agnostic.
        parsed = _make(vendor="UPS", carrier="UPS")
        self.assertTrue(inbox._is_carrier_notification(parsed, "ups.com"))

    def test_vendor_equals_carrier_case_insensitive(self) -> None:
        parsed = _make(vendor="FedEx", carrier="fedex")
        self.assertTrue(inbox._is_carrier_notification(parsed, None))

    def test_known_carrier_sender_domain_is_a_carrier_email(self) -> None:
        # Sender domain matches the carrier-domain set, even when the parser
        # extracted a less-conclusive vendor field.
        parsed = _make(vendor="UPS Quantum View", carrier=None)
        self.assertTrue(inbox._is_carrier_notification(parsed, "ups.com"))

    def test_carrier_subdomain_matches(self) -> None:
        # pkginfo.ups.com, mail.fedex.com etc. should still match the suffix.
        parsed = _make(vendor=None, carrier=None)
        self.assertTrue(inbox._is_carrier_notification(parsed, "pkginfo.ups.com"))

    def test_vendor_email_not_a_carrier(self) -> None:
        parsed = _make(vendor="FUTEK Advanced Sensor Technology, Inc.",
                       carrier="UPS")
        # Vendor != carrier and sender_domain is the order vendor's domain.
        self.assertFalse(inbox._is_carrier_notification(parsed, "futek.com"))

    def test_empty_fields_not_a_carrier(self) -> None:
        # Neither signal fires when everything is blank.
        parsed = _make()
        self.assertFalse(inbox._is_carrier_notification(parsed, None))
        self.assertFalse(inbox._is_carrier_notification(parsed, ""))

    def test_both_empty_strings_not_a_match(self) -> None:
        # "" == "" must not count as a vendor==carrier match.
        parsed = _make(vendor="", carrier="")
        self.assertFalse(inbox._is_carrier_notification(parsed, None))


if __name__ == "__main__":
    unittest.main()
