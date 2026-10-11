import importlib
import unittest

module = importlib.import_module("scripts.coordinates.regional-overture-identity")


class OvertureIdentityTests(unittest.TestCase):
    def setUp(self):
        self.row = {"names": ["QUIKTRIP #598"], "City": "Gretna", "State": "NE"}
        self.published = {"name": "QuikTrip #598", "street": "11105 Sapp Brothers Drive"}
        self.poi = {
            "name": "QuikTrip",
            "latitude": 41.148,
            "longitude": -96.161,
            "addresses": [
                {
                    "freeform": "11105 Sapp Brothers Dr",
                    "locality": "Gretna",
                    "region": "NE",
                    "country": "US",
                }
            ],
        }

    def matches(self, inside=False):
        return module.qualifying_addresses(self.row, self.published, self.poi, lambda *_: inside)

    def test_chain_brand_requires_exact_full_published_street(self):
        self.assertEqual(len(self.matches()), 1)
        self.poi["addresses"][0]["freeform"] = "11205 Wickersham Blvd"
        self.assertEqual(self.matches(), [])

    def test_different_store_number_rejected_even_at_same_street(self):
        self.poi["name"] = "QuikTrip #599"
        self.assertEqual(self.matches(), [])

    def test_different_chain_cannot_be_assumed_rebrand(self):
        self.poi["name"] = "Kum & Go"
        self.assertEqual(self.matches(), [])

    def test_primary_published_pump_brand_at_exact_street_is_a_review_lead(self):
        self.row["names"] = ["Stripes #40705"]
        self.published["name"] = "Stripes #40705"
        self.poi["name"] = "Valero"
        self.assertEqual(self.matches(), [])
        self.published["fuel_brand"] = "Valero"
        self.assertEqual(len(self.matches()), 1)
        self.poi["addresses"][0]["freeform"] = "21379 US 77"
        self.assertEqual(self.matches(), [])

    def test_explicit_other_city_rejected_even_inside_expected_boundary(self):
        self.poi["addresses"][0]["locality"] = "Omaha"
        self.assertEqual(self.matches(inside=True), [])

    def test_missing_city_needs_actual_city_boundary(self):
        self.poi["addresses"][0]["locality"] = ""
        self.assertEqual(self.matches(), [])
        self.assertEqual(len(self.matches(inside=True)), 1)

    def test_street_without_house_number_is_not_identity(self):
        self.published["street"] = "Sapp Brothers Drive"
        self.poi["addresses"][0]["freeform"] = "Sapp Brothers Dr"
        self.assertEqual(self.matches(), [])

    def test_wrong_country_and_state_are_rejected(self):
        for field, value in [("country", "CA"), ("region", "IA")]:
            with self.subTest(field=field):
                original = self.poi["addresses"][0][field]
                self.poi["addresses"][0][field] = value
                self.assertEqual(self.matches(), [])
                self.poi["addresses"][0][field] = original

    def test_distinctive_independent_name_must_agree(self):
        self.row["names"] = ["Buchs Truck Stop"]
        self.published["name"] = "Buch's Truck Stop"
        self.poi["name"] = "Buch's Truck Stop"
        self.assertEqual(len(self.matches()), 1)
        self.poi["name"] = "Restaurant"
        self.assertEqual(self.matches(), [])

    def test_invalid_coordinate_is_rejected(self):
        for value in (float("nan"), None, "41.148"):
            with self.subTest(value=value):
                self.poi["latitude"] = value
                self.assertEqual(self.matches(), [])

    def test_direct_primary_store_number_must_agree_with_native_title(self):
        row = {
            "Status": "Open",
            "Store: Store Name": "Stripes-40915",
            "Store #": "40915",
            "Address": "14118 Mines Rd",
            "City": "Laredo",
            "State": "TX",
            "Long Lat (Latitude)": "27.6",
            "Long Lat (Longitude)": "-99.5",
        }
        result = list(module.sevenfleet_addresses([row]))
        self.assertEqual(result[0]["name"], "Stripes #40915")
        self.assertNotIn("latitude", result[0])
        self.assertNotIn("longitude", result[0])
        row["Store #"] = "40916"
        self.assertEqual(list(module.sevenfleet_addresses([row])), [])


if __name__ == "__main__":
    unittest.main()
