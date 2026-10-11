import math
import unittest

from scripts.coordinates.exit_brand_recovery import (
    SOURCES,
    branch_identity,
    fuel_features,
    prepare,
    route_branch_proposals,
    strip_tokens,
)


def feature(**updates):
    return {
        "properties": {
            "ref": "0382354",
            "brand": "Chevron",
            "addr:country": "US",
            "amenity": "fuel",
            "addr:street_address": "1366 RUSH ROAD",
            "addr:city": "Chehalis",
            "addr:state": "WA",
            **updates,
        },
        "geometry": {"type": "Point", "coordinates": [-122.9, 46.6]},
    }


def row(**updates):
    return {
        "opis_id": 2817,
        "names": ["Chevron #382354"],
        "address": "I-5/US-12, EXIT 72",
        "city": "Chehalis",
        "state": "WA",
        **updates,
    }


class BranchTests(unittest.TestCase):
    def test_exact_store_number_and_city_state_identity(self):
        self.assertEqual(
            branch_identity(row(), feature()), "same_current_primary_brand_store_number_city_state"
        )

    def test_wrong_number_brand_city_or_state_cannot_fall_back_to_proximity(self):
        for updates in (
            {"ref": "382355"},
            {"brand": "Texaco"},
            {"addr:city": "Centralia"},
            {"addr:state": "OR"},
            {"ref": "382354-store"},
        ):
            with self.subTest(updates=updates):
                self.assertIsNone(branch_identity(row(), feature(**updates)))

    def test_wrong_store_number_cannot_fall_back_to_same_street(self):
        self.assertIsNone(branch_identity(row(address="1366 Rush Rd"), feature(ref="99")))

    def test_bare_brand_exit_or_route_is_not_station_identity(self):
        self.assertIsNone(branch_identity(row(names=["Chevron"]), feature()))
        self.assertIsNone(branch_identity(row(names=["Chevron"], address="US-12"), feature()))

    def test_numeric_street_can_bind_primary_branch_without_store_number(self):
        self.assertEqual(
            branch_identity(row(names=["Chevron"], address="1366 Rush Rd"), feature()),
            "same_current_primary_brand_numeric_street_city_state",
        )
        self.assertIsNone(
            branch_identity(row(names=["Chevron"], address="1368 Rush Rd"), feature())
        )

    def test_attached_shops_foreign_fuel_ev_and_invalid_geometry_excluded(self):
        invalid = [
            feature(amenity="charging_station"),
            feature(amenity="", shop="convenience"),
            feature(**{"addr:country": "CA"}),
        ]
        invalid.append({**feature(), "geometry": {"type": "Point", "coordinates": [math.nan, 40]}})
        invalid.append({**feature(), "geometry": {"type": "Point", "coordinates": [-190, 40]}})
        self.assertEqual(list(fuel_features({"features": [feature(), *invalid]})), [feature()])

    def test_nested_published_tokens_removed_without_losing_public_filters(self):
        value = {
            "evidence": [
                "https://operator.test/api?clientid=published&lat=1&KEY=published&limit=50"
            ]
        }
        self.assertEqual(
            strip_tokens(value), {"evidence": ["https://operator.test/api?lat=1&limit=50"]}
        )

    def test_invalid_collection_rejected(self):
        with self.assertRaises(TypeError):
            list(fuel_features({"stations": []}))

    def test_matching_branch_and_nearby_fuel_remain_unverified_leads(self):
        mapped = {
            "type": "way",
            "id": 123,
            "latitude": 46.6,
            "longitude": -122.9,
            "tags": {
                "amenity": "fuel",
                "brand": "Shell",
                "addr:housenumber": "1366",
                "addr:street": "Rush Rd",
                "website": "https://operator.test/?api_key=published&branch=123",
            },
        }
        proposals = prepare(
            [row()],
            [
                (
                    SOURCES / "test-feed.json",
                    {
                        "features": [feature()],
                        "source_url": "https://operator.test/?clientid=published&branch=382354",
                    },
                )
            ],
            {"WA": [mapped]},
        )
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertFalse(proposal["coordinate_verified"])
        self.assertNotIn("latitude", proposal)
        self.assertEqual(proposal["dataset_url"], "https://operator.test/?branch=382354")
        lead = proposal["nearby_mapped_fuel_leads"][0]
        self.assertTrue(lead["same_published_street"])
        self.assertFalse(lead["coordinate_verified"])
        self.assertEqual(lead["point"]["tags"]["brand"], "Shell")
        self.assertEqual(lead["point"]["tags"]["website"], "https://operator.test/?branch=123")

    def test_duplicate_current_branch_id_stays_ambiguous_in_proposals(self):
        proposals = prepare(
            [row()],
            [
                (
                    SOURCES / "test-feed.json",
                    {"features": [feature(), feature(**{"addr:street_address": "1380 Rush Rd"})]},
                )
            ],
            {},
        )
        self.assertEqual(len(proposals), 2)
        self.assertTrue(all(not item["coordinate_verified"] for item in proposals))
        self.assertEqual(
            {item["primary_branch"]["properties"]["addr:street_address"] for item in proposals},
            {"1366 RUSH ROAD", "1380 Rush Rd"},
        )

    def test_exit_proximity_retains_all_primary_choices_without_acceptance(self):
        original = row(names=["Chevron"], address="I-5, EXIT 72")
        exit_proposal = {
            "opis_id": 2817,
            "request": {"routes": ["I-5"], "exit_ref": "72"},
            "anchors": [
                {
                    "state": "WA",
                    "route": "I-5",
                    "exit_ref": "72",
                    "latitude": 46.6,
                    "longitude": -122.9,
                }
            ],
            "fuel_candidates": [],
        }
        feeds = [
            (
                SOURCES / "test-feed.json",
                {
                    "features": [
                        feature(),
                        feature(ref="999", **{"addr:street_address": "1380 Rush Rd"}),
                    ]
                },
            )
        ]
        proposals = route_branch_proposals([original], [exit_proposal], feeds)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(len(proposals[0]["primary_branch_choices"]), 2)
        self.assertFalse(proposals[0]["identity_verified"])
        self.assertFalse(proposals[0]["coordinate_verified"])
        self.assertTrue(
            all(not item["identity_verified"] for item in proposals[0]["primary_branch_choices"])
        )
        self.assertEqual(route_branch_proposals([row()], [exit_proposal], feeds), [])
        self.assertEqual(route_branch_proposals([original], [exit_proposal], feeds, [2817]), [])
        for anchor_change in ({"state": "OR"}, {"route": "I-84"}, {"exit_ref": "72A"}):
            with self.subTest(anchor_change=anchor_change):
                other = {
                    **exit_proposal,
                    "anchors": [{**exit_proposal["anchors"][0], **anchor_change}],
                }
                self.assertEqual(route_branch_proposals([original], [other], feeds), [])
        changed_exit = {**exit_proposal, "request": {"routes": ["I-5"], "exit_ref": "73"}}
        self.assertEqual(route_branch_proposals([original], [changed_exit], feeds), [])


if __name__ == "__main__":
    unittest.main()
