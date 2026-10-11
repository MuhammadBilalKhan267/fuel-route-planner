"""Check native identity extraction without accepting unreviewed operator pins."""

import importlib
import json
import unittest

module = importlib.import_module("scripts.coordinates.regional-street")


class StreetDirectoryTests(unittest.TestCase):
    def test_enmarket_preserves_native_identity_and_flags_unreviewed_pin(self):
        row = {
            "id": 12,
            "title": "Example, GA - 1329",
            "street": "1264 US Hwy 80 E",
            "city": "Example",
            "state": "GA",
            "zip": "31322",
            "country": "US",
            "phone": "+1 555-555-5555",
            "amenities": ["Diesel"],
            "latitude": "32.12543",
            "longitude": "-81.27711",
        }
        result = module.enmarket_locations(
            "var map_info = " + json.dumps({"map_info": [row]}) + "; irrelevant;"
        )[0]
        self.assertEqual((result["external_id"], result["store_number"]), ("12", "1329"))
        self.assertEqual(result["street"], "1264 US Hwy 80 E")
        self.assertFalse(result["coordinate_verified"])

    def test_jet_identity_does_not_leak_next_store_coordinates(self):
        documents = []
        for native, store, city, lat in [
            (7218, "04", "Wadley", 32.86),
            (7219, "05", "Waynesboro", 33.09),
        ]:
            documents.append(
                f'<li id="location-{native}" data-loc-id="{native}" class="location-listing">'
                f'<span class="loc-address"><strong>Store #{store}:</strong> 10550 U.S. 1, {city}, GA</span>'
                f'<a href="https://www.google.com/maps/dir//10550+U.S.+1/@{lat},-82.39,17z/">Directions</a>'
                '<div class="loc_brand marathon"></div>'
                '<a href="tel:4782525951">Phone</a></li>'
            )
        result = module.jet_locations("".join(documents))
        self.assertEqual([row["city"] for row in result], ["Wadley", "Waynesboro"])
        self.assertEqual([row["latitude"] for row in result], [32.86, 33.09])
        self.assertFalse(result[0]["coordinate_verified"])
        self.assertEqual(result[0]["fuel_brand"], "marathon")

    def test_jet_missing_primary_coordinate_is_not_silently_skipped(self):
        with self.assertRaisesRegex(ValueError, "Incomplete primary Jet location"):
            module.jet_locations('<li id="location-7" class="location-listing"><span></span>')

    def test_hutch_keeps_store_address_and_unverified_marker_together(self):
        document = (
            '<h3>Hutch\'s #125</h3><img alt="diesel-icon"><img alt="truck-icon">'
            '<div class="marker" data-lat="34.667165" data-lng="-99.317863">'
            '<a href="https://www.hutchs.net/1301-e-tamarack/">'
            '<h4 class="store-number">Hutch\'s #125</h4>'
            '<div class="address">1301 East Tamarack Road, Altus, OK, USA</div>'
            '<div class="phone">580-379-0107</div></a></div>'
        )
        result = module.hutch_locations(document)[0]
        self.assertEqual(result["store_number"], "125")
        self.assertEqual(
            (result["street"], result["city"], result["state"]),
            ("1301 East Tamarack Road", "Altus", "OK"),
        )
        self.assertIn("truck-icon", result["amenities"])
        self.assertFalse(result["coordinate_verified"])

    def test_hutch_invalid_pin_preserves_address_identity(self):
        document = (
            '<div class="marker" data-lat="nan" data-lng="-99.3">'
            '<a href="https://www.hutchs.net/example/">'
            "<h4>Hutch's #125</h4>"
            '<div class="address">1301 East Tamarack Road, Altus, OK 73521, USA</div>'
            "</a></div>"
        )
        result = module.hutch_locations(document)[0]
        self.assertEqual(result["postal_code"], "73521")
        self.assertIsNone(result["latitude"])
        self.assertIsNone(result["longitude"])


if __name__ == "__main__":
    unittest.main()
