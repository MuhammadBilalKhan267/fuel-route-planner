"""Offline identity checks for coordinate-enrichment evidence rules."""

from __future__ import annotations

import math
import unittest
from unittest.mock import patch

import independent_identity as matcher


def station(opis_id: int = 1, **fields) -> dict:
    return {
        "opis_id": opis_id,
        "names": ["Olive Exxon"],
        "city": "Holland",
        "state": "MI",
        "address": "US-31",
        **fields,
    }


def source(**fields) -> dict:
    return {
        "name": "Olive Exxon",
        "address": "6363 136th Ave",
        "city": "Holland",
        "state": "MI",
        "latitude": 42.884895,
        "longitude": -86.12041,
        "highway_address": "US-31",
        "source": "fuelman_public_station_locator",
        "external_id": "983570",
        "source_url": "https://sitelocator.fuelman.com/",
        **fields,
    }


def premises(**fields) -> dict:
    tags = {
        "amenity": "fuel",
        "brand": "Exxon",
        "addr:housenumber": "6363",
        "addr:street": "136th Avenue",
        "addr:city": "Holland",
        "addr:state": "MI",
    }
    tags.update(fields.pop("tags", {}))
    return {
        "type": "node",
        "id": 8238942753,
        "name": "Exxon",
        "city": tags.get("addr:city", ""),
        "latitude": 42.884958,
        "longitude": -86.1204542,
        "tags": tags,
        "source_url": "https://www.openstreetmap.org/node/8238942753",
        **fields,
    }


class IdentityRules(unittest.TestCase):
    def test_exact_operator_number_bridge(self):
        for left in ("MFA #911251", "Petro-Card 24 #911251"):
            self.assertEqual(
                matcher.name_rule(left, "MFA PETRO24 911251", "King City"),
                "same_published_mfa_petrocard_store_number",
            )

    def test_numbers_and_networks_are_not_interchangeable(self):
        for left, right in (
            ("Petro-Card 24 #919141", "MFA PETRO24 910141"),
            ("MFA #2028", "PETRO CARD 24 1067"),
            ("Flyers #3200", "Flyers #3201"),
            ("Olive Exxon #12", "Olive Exxon #13"),
            ("PWI #911251", "MFA PETRO24 911251"),
        ):
            self.assertIsNone(matcher.name_rule(left, right, "Holland"))

    def test_street_normalization_preserves_directions_and_numbers(self):
        self.assertEqual(matcher.street_key("204 North Connecticut Street"), "204nconnecticutst")
        self.assertNotEqual(
            matcher.street_key("193 NJ17 North"), matcher.street_key("198 NJ17 South")
        )
        self.assertNotEqual(
            matcher.street_key("123 North Main Street"), matcher.street_key("123 South Main Street")
        )

    def test_premises_reject_explicit_city_state_or_address_mismatch(self):
        for tags in (
            {"addr:city": "Zeeland"},
            {"addr:state": "IN"},
            {"addr:housenumber": "6364", "name": "Olive Exxon"},
            {"addr:street": "North 136th Avenue", "name": "Olive Exxon"},
        ):
            self.assertIsNone(matcher.premise_rule(source(), premises(tags=tags), "Holland", "MI"))

    def test_exact_street_can_bridge_operator_and_fuel_brand(self):
        self.assertEqual(
            matcher.premise_rule(source(), premises(), "Holland", "MI"),
            "same_complete_street_at_mapped_station_premises",
        )

    def test_matching_address_is_not_enough_without_station_premises(self):
        point = premises(tags={"amenity": "school", "brand": "", "name": ""})
        self.assertIsNone(matcher.premise_rule(source(), point, "Holland", "MI"))

    @patch.object(matcher, "read", return_value=[])
    def test_matching_directories_without_physical_evidence_are_unresolved(self, _read):
        snap = source(source="official_usda_snap_retailer_coordinate", external_id="usda:1")
        accepted, unresolved = matcher.resolve([station()], [source(), snap], [])
        self.assertEqual(accepted, [])
        self.assertEqual(len(unresolved), 1)

    @patch.object(matcher, "read", return_value=[])
    def test_returned_point_is_the_physical_station(self, _read):
        point = premises()
        accepted, _unresolved = matcher.resolve([station()], [source()], [point])
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["latitude"], point["latitude"])
        self.assertEqual(accepted[0]["longitude"], point["longitude"])

    @patch.object(matcher, "read", return_value=[])
    def test_shared_external_identity_does_not_merge_opis_ids(self, _read):
        accepted, unresolved = matcher.resolve([station(1), station(2)], [source()], [premises()])
        self.assertEqual(accepted, [])
        self.assertEqual(
            {
                row["opis_id"]
                for row in unresolved
                if row["reason"] == "same_source_matched_multiple_opis_ids"
            },
            {1, 2},
        )

    @patch.object(matcher, "read", return_value=[])
    def test_invalid_or_out_of_country_coordinates_are_rejected(self, _read):
        for value in (None, math.nan, math.inf, 0, 80):
            accepted, _unresolved = matcher.resolve(
                [station()], [source(latitude=value)], [premises()]
            )
            self.assertEqual(accepted, [])

    @patch.object(matcher, "read", return_value=[])
    def test_explicit_source_city_and_state_are_required(self, _read):
        for fields in ({"city": "Zeeland"}, {"state": "IN"}):
            accepted, _unresolved = matcher.resolve([station()], [source(**fields)], [premises()])
            self.assertEqual(accepted, [])


if __name__ == "__main__":
    unittest.main()
