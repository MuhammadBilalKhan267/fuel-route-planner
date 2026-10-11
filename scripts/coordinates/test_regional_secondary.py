"""Regression checks for conservative regional station-identity ingestion."""

import importlib
import unittest
from unittest.mock import patch

regional = importlib.import_module("scripts.coordinates.regional-secondary")


class RegionalIdentityTests(unittest.TestCase):
    def setUp(self):
        self.row = {
            "names": ["PUMP & PANTRY #33"],
            "City": "Genoa",
            "State": "NE",
            "Address": "SR-39",
        }
        self.point = {
            "brand": "pumpandpantry",
            "store_number": "33",
            "name": "Pump & Pantry #33",
            "address": "609 Willard Ave",
            "city": "Genoa",
            "state": "NE",
            "latitude": 41.448181,
            "longitude": -97.732195,
            "source_url": "https://pumpandpantry.com/store/pump-pantry-33/",
            "external_id": "pumpandpantry:33",
        }

    def result(self, points, identities=()):
        outputs = {}
        with (
            patch.object(regional, "operator_points", return_value=points),
            patch.object(regional, "address_identities", return_value=list(identities)),
            patch.object(regional, "stations", return_value={71662: self.row}),
            patch.object(regional, "write", side_effect=outputs.__setitem__),
        ):
            regional.main()
        return outputs["regional-secondary-candidates.json"], outputs[
            "regional-secondary-unresolved.json"
        ]

    def test_exact_store_identity_accepts_operator_premises(self):
        accepted, unresolved = self.result([self.point])
        self.assertEqual([item["opis_id"] for item in accepted], [71662])
        self.assertEqual(unresolved, [])
        self.assertEqual(accepted[0]["matched_address"], "609 Willard Ave")

    def test_same_number_at_another_brand_is_rejected(self):
        accepted, _ = self.result([{**self.point, "brand": "thorntons"}])
        self.assertEqual(accepted, [])

    def test_explicit_different_city_is_not_a_nearby_city_fallback(self):
        accepted, _ = self.result([{**self.point, "city": "Columbus"}])
        self.assertEqual(accepted, [])

    def test_different_store_number_at_same_city_is_rejected(self):
        accepted, _ = self.result([{**self.point, "store_number": "34"}])
        self.assertEqual(accepted, [])

    def test_duplicate_locator_fields_on_same_property_are_one_site(self):
        accepted, unresolved = self.result([self.point, {**self.point, "latitude": 41.44825}])
        self.assertEqual(len(accepted), 1)
        self.assertEqual(unresolved, [])

    def test_distinct_addresses_are_not_collapsed_across_the_road(self):
        accepted, unresolved = self.result(
            [self.point, {**self.point, "address": "610 Willard Ave", "latitude": 41.44825}]
        )
        self.assertEqual(accepted, [])
        self.assertEqual(len(unresolved[0]["possible_sites"]), 2)

    def test_displaced_coordinates_for_same_address_remain_ambiguous(self):
        accepted, unresolved = self.result([self.point, {**self.point, "latitude": 41.46}])
        self.assertEqual(accepted, [])
        self.assertEqual(len(unresolved[0]["possible_sites"]), 2)

    def test_published_address_cannot_bridge_the_wrong_store(self):
        identity = {
            "name": "Pump & Pantry #34",
            "address": "609 Willard Ave",
            "city": "Genoa",
            "state": "NE",
            "source_url": "https://example.org/retailer",
            "external_id": "34",
        }
        accepted, _ = self.result([{**self.point, "store_number": None}], [identity])
        self.assertEqual(accepted, [])

    def test_retailer_numbers_with_leading_zeroes_are_canonical(self):
        self.assertEqual(regional.number("Town Pump of Boulder 0310"), "310")
        self.assertEqual(regional.number("Cubbys North Cliff 406"), "406")


if __name__ == "__main__":
    unittest.main()
