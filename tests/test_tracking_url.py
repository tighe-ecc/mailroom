"""Unit tests for the `tracking_url_for` Jinja filter.

Covers the Protolabs bug: a shipping confirmation got an LLM-hallucinated
d.digikey.com tracking_url written into the DB. The stored URL was being
returned verbatim, sending the user to DigiKey instead of FedEx.

Run: .venv/bin/python -m unittest tests.test_tracking_url -v
"""

from __future__ import annotations

import unittest

from mailroom import scrape


class TrackingUrlForTests(unittest.TestCase):
    # ---------- the Protolabs regression itself ----------

    def test_digikey_url_on_fedex_protolabs_falls_back_to_fedex(self) -> None:
        pkg = {
            "vendor": "Protolabs",
            "carrier": "FedEx",
            "tracking_number": "381386875690",
            "tracking_url": "https://d.digikey.com/dc/abc123",
        }
        url = scrape.tracking_url_for(pkg)
        self.assertIsNotNone(url)
        assert url is not None
        self.assertIn("fedex.com", url)
        self.assertIn("381386875690", url)

    # ---------- legitimate stored URLs are preserved ----------

    def test_real_fedex_url_is_kept(self) -> None:
        pkg = {
            "vendor": "StepperOnline",
            "carrier": "FedEx",
            "tracking_number": "871354287114",
            "tracking_url": "https://www.fedex.com/fedextrack/?trknbr=871354287114",
        }
        self.assertEqual(
            scrape.tracking_url_for(pkg),
            "https://www.fedex.com/fedextrack/?trknbr=871354287114",
        )

    def test_real_ups_url_is_kept(self) -> None:
        pkg = {
            "vendor": "FUTEK",
            "carrier": "UPS",
            "tracking_number": "1Z90W3240262341201",
            "tracking_url": "https://www.ups.com/track?tracknum=1Z90W3240262341201",
        }
        url = scrape.tracking_url_for(pkg)
        self.assertEqual(url, "https://www.ups.com/track?tracknum=1Z90W3240262341201")

    def test_vendor_portal_url_kept_for_carrier_without_trusted_hosts(self) -> None:
        # Uline ships LTL freight under "MOTOR FREIGHT - TOTAL TRANSPORT" and
        # links to its own portal. We have no template/trusted-host map for
        # that carrier, so the stored URL must win.
        pkg = {
            "vendor": "Uline",
            "carrier": "MOTOR FREIGHT - TOTAL TRANSPORT",
            "tracking_number": "9706828",
            "tracking_url": "https://uline.com/MyAccount/Tracking?p=1&o=2&c=3",
        }
        self.assertEqual(
            scrape.tracking_url_for(pkg),
            "https://uline.com/MyAccount/Tracking?p=1&o=2&c=3",
        )

    def test_digikey_wrapper_url_kept_on_real_digikey_shipment(self) -> None:
        # DigiKey-vendored shipments arrive over FedEx but the email points at
        # a d.digikey.com click-tracker that does resolve to the right
        # shipment. Because the carrier IS FedEx, this looks identical to the
        # Protolabs failure mode from `tracking_url_for`'s perspective — and
        # the right behavior is the same: route to FedEx with the real
        # tracking number. The user can still click through the DigiKey wrap
        # from the order email itself.
        pkg = {
            "vendor": "DigiKey",
            "carrier": "FedEx",
            "tracking_number": "523372461851",
            "tracking_url": "https://d.digikey.com/dc/abc",
        }
        url = scrape.tracking_url_for(pkg)
        self.assertIsNotNone(url)
        assert url is not None
        self.assertIn("fedex.com", url)
        self.assertIn("523372461851", url)

    # ---------- carrier sniffing from tracking-number format ----------

    def test_sniff_fedex_from_12_digit_when_carrier_missing(self) -> None:
        pkg = {"carrier": None, "tracking_number": "381386875690"}
        url = scrape.tracking_url_for(pkg)
        self.assertIsNotNone(url)
        assert url is not None
        self.assertIn("fedex.com", url)

    def test_sniff_ups_from_1z_when_carrier_missing(self) -> None:
        pkg = {"carrier": "", "tracking_number": "1Z12X6330394850268"}
        url = scrape.tracking_url_for(pkg)
        self.assertIsNotNone(url)
        assert url is not None
        self.assertIn("ups.com", url)

    def test_no_carrier_no_template_returns_none(self) -> None:
        pkg = {"carrier": None, "tracking_number": "ABC123XYZ"}
        self.assertIsNone(scrape.tracking_url_for(pkg))

    def test_no_tracking_number_no_stored_url_returns_none(self) -> None:
        pkg = {"carrier": "FedEx", "tracking_number": None, "tracking_url": None}
        self.assertIsNone(scrape.tracking_url_for(pkg))

    # ---------- DHLExpress carrier string (no space) ----------

    def test_dhlexpress_carrier_routes_to_dhl(self) -> None:
        pkg = {"carrier": "DHL Express", "tracking_number": "2929429613"}
        url = scrape.tracking_url_for(pkg)
        self.assertIsNotNone(url)
        assert url is not None
        self.assertIn("dhl.com", url)
        self.assertIn("2929429613", url)


if __name__ == "__main__":
    unittest.main()
