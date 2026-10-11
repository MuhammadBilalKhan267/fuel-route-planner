import json
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import SimpleTestCase
from shapely.geometry import Polygon

from scripts.coordinates.aerial_tiles import matches_cached_location, pixel_coordinate
from scripts.coordinates.fuelman import station_records
from scripts.coordinates.match_osm import distinct_sites, identity, same_place
from scripts.coordinates.matching import normalized, store_number
from scripts.coordinates.merge import (
    identity_collisions,
    physical_aliases,
    reconcile,
    rejected_candidates,
    reviewed_point_rejections,
    unresolved_identity_reviews,
)
from scripts.coordinates.places import inside_named_place
from scripts.coordinates.premises import (
    epa_address_identities,
    numeric_street,
    published_identities,
    street_matches,
    valid_geometry,
)
from scripts.coordinates.snap import current_records
from scripts.coordinates.street_anchors import directory_identities
from trips.importing import refresh_revision
from trips.models import FuelStation
from trips.stations import dataset_status, station_index

from .helpers import CachedTestCase, create_station


class CoordinateIdentityTests(SimpleTestCase):
    def test_current_numbered_directory_supplies_identity_without_exporting_its_pin(self):
        record = {
            "name": "Stripes #40915",
            "street": "14118 Mines Road",
            "city": "Laredo",
            "state": "TX",
            "source_url": "https://example.com/current-operator-directory.csv",
            "external_id": "7fleet:40915",
            "latitude": 27.7,
            "longitude": -99.5,
            "coordinate_verified": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in (
                "fuelman-stations.json",
                "supplemental-comdata-addresses.json",
                "regional-nrha-merchant-addresses.json",
            ):
                (folder / name).write_text("[]")
            (folder / "regional-7fleet-address-identities.json").write_text(json.dumps([record]))
            with patch("scripts.coordinates.premises.SOURCES", folder):
                actual = list(published_identities({}, {}))
        self.assertEqual(
            actual,
            [
                {
                    key: record[key]
                    for key in ("name", "street", "city", "state", "source_url", "external_id")
                }
            ],
        )
        self.assertNotIn("latitude", actual[0])
        self.assertNotIn("longitude", actual[0])

    def test_aerial_cache_cannot_reuse_a_wrong_property_or_zoom(self):
        metadata = {"search_coordinate": [-77.66, 37.7], "zoom": 17}
        self.assertTrue(matches_cached_location(metadata, 37.7, -77.66, 17))
        self.assertFalse(matches_cached_location(metadata, 38.96, -77.50, 17))
        self.assertFalse(matches_cached_location(metadata, 37.7, -77.66, 18))
        self.assertFalse(matches_cached_location({}, 37.7, -77.66, 17))

    def test_aerial_pixel_uses_mercator_latitude(self):
        latitude, longitude = pixel_coordinate(0, 0, 1, 1, 2)
        self.assertAlmostEqual(latitude, 66.5132604431, places=9)
        self.assertEqual(longitude, -90)
        self.assertEqual(pixel_coordinate(256, 256, 1, 1, 2), (0, 0))

    def test_bad_point_can_be_quarantined_before_correct_premises_are_known(self):
        candidate = self.candidate("fuelman_public_station_locator")
        review = {
            **candidate,
            "opis_id": 1,
            "reason": "Aerial imagery and the published street show that this point is woodland.",
            "evidence": [{"source_url": "https://example.com/aerial", "pixel_center": [50, 50]}],
        }
        indexed = reviewed_point_rejections([review], {1: {"State": "TX"}})
        self.assertEqual(indexed[1], [review])
        changed_point = {**candidate, "longitude": -97.1}
        other_id = {**candidate, "external_id": "43"}
        self.assertEqual(
            rejected_candidates(
                [candidate, changed_point, other_id],
                {"source": "manually_reviewed_station_premises", "rejected_candidates": indexed[1]},
            ),
            {0},
        )
        for changed in ({"evidence": []}, {"external_id": ""}, {"latitude": float("nan")}):
            with self.assertRaises(ValueError):
                reviewed_point_rejections([{**review, **changed}], {1: {"State": "TX"}})

    def test_directory_street_join_requires_identity_and_one_distinct_address(self):
        row = {"names": ["Pilot #42"], "City": "Sterling", "State": "OK"}
        record = {
            "name": "Pilot #42",
            "street": "100 Main Street",
            "city": "Sterling",
            "state": "OK",
            "source_url": "https://example.com/pilot/42",
            "external_id": "42",
        }
        with patch(
            "scripts.coordinates.street_anchors.published_identities",
            return_value=[record, {**record, "street": "100 Main St"}],
        ):
            accepted, ambiguous = directory_identities({1}, {1: row})
        self.assertEqual([item["opis_id"] for item in accepted], [1])
        self.assertFalse(ambiguous)
        with patch(
            "scripts.coordinates.street_anchors.published_identities",
            return_value=[record, {**record, "street": "200 Other Road"}],
        ):
            accepted, ambiguous = directory_identities({1}, {1: row})
        self.assertFalse(accepted)
        self.assertEqual([item["opis_id"] for item in ambiguous], [1])
        with patch(
            "scripts.coordinates.street_anchors.published_identities",
            return_value=[{**record, "name": "Love's #42"}],
        ):
            self.assertEqual(directory_identities({1}, {1: row}), ([], []))

    def test_physical_alias_requires_known_canonical_member_and_same_state(self):
        rows = {1: {"State": "TX"}, 2: {"State": "TX"}, 3: {"State": "OK"}}
        site = {
            "opis_ids": [1, 2],
            "canonical_opis_id": 1,
            "evidence": "Official operator record and identical street prove the same premises.",
            "source_url": "https://example.com/station/42",
        }
        self.assertEqual(physical_aliases([site], rows), {1: 1, 2: 1})
        for changed in (
            {"canonical_opis_id": 3},
            {"opis_ids": [1]},
            {"opis_ids": [1, 3]},
            {"opis_ids": [1, 99]},
            {"opis_ids": [1, 1, 2]},
            {"source_url": ""},
        ):
            with self.assertRaises(ValueError):
                physical_aliases([{**site, **changed}], rows)
        with self.assertRaises(ValueError):
            physical_aliases([site, site], rows)

    def test_unresolved_identity_review_is_bound_to_original_input(self):
        rows = {1: {"Address": "I-84 Exit 16B", "City": "Jerome", "State": "ID"}}
        review = {
            "opis_id": 1,
            "original_address": "I-84 Exit 16B",
            "original_city": "Jerome",
            "original_state": "ID",
            "source_url": "https://example.com/two-stations",
            "reason": "Two distinct premises exist and the supplied exit does not identify one.",
        }
        self.assertEqual(unresolved_identity_reviews([review], rows), {1: review})
        for changed in ({"original_address": "Changed"}, {"opis_id": 99}, {"reason": ""}):
            with self.assertRaises(ValueError):
                unresolved_identity_reviews([{**review, **changed}], rows)
        with self.assertRaises(ValueError):
            unresolved_identity_reviews([review, review], rows)

    def test_named_place_boundary_rejects_nearby_other_town_and_missing_city(self):
        row = {"State": "PA", "City": "Saint Martin"}
        places = {("PA", "stmartin"): [Polygon([(-79, 40), (-78, 40), (-78, 41), (-79, 41)])]}
        self.assertTrue(inside_named_place(row, {"longitude": -78.5, "latitude": 40.5}, places))
        self.assertFalse(inside_named_place(row, {"longitude": -77.999, "latitude": 40.5}, places))
        self.assertFalse(
            inside_named_place(
                {**row, "City": "Other"}, {"longitude": -78.5, "latitude": 40.5}, places
            )
        )

    def test_public_retailer_export_rejects_missing_truncated_and_duplicate_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with self.assertRaisesMessage(ValueError, "missing"):
                current_records(folder)
            page = folder / "000000.json"
            page.write_text(
                json.dumps(
                    {"features": [{"attributes": {"ObjectId": 1}}], "exceededTransferLimit": True}
                )
            )
            with self.assertRaisesMessage(ValueError, "incomplete"):
                current_records(folder)
            (folder / "002000.json").write_text(json.dumps({"features": []}))
            with self.assertRaises(ValueError):
                current_records(folder)
            (folder / "002000.json").unlink()
            page.write_text(
                json.dumps(
                    {"features": [{"attributes": {"ObjectId": 1}}, {"attributes": {"ObjectId": 1}}]}
                )
            )
            with self.assertRaisesMessage(ValueError, "duplicate"):
                current_records(folder)
            page.write_text(json.dumps({"features": [{"attributes": {"ObjectId": 1}}]}))
            self.assertEqual(current_records(folder), [{"ObjectId": 1}])

    def test_addressed_premises_reject_missing_nonpoint_and_nonfinite_geometry(self):
        for feature in (
            {},
            {"geometry": None},
            {"geometry": {"type": "LineString", "coordinates": [1, 2]}},
            {"geometry": {"type": "Point", "coordinates": [None, 35]}},
            {"geometry": {"type": "Point", "coordinates": [float("nan"), 35]}},
        ):
            self.assertFalse(valid_geometry(feature))
        self.assertTrue(valid_geometry({"geometry": {"type": "Point", "coordinates": [-97, 35]}}))
        self.assertTrue(numeric_street("21W400 North Ave"))
        self.assertTrue(numeric_street("123 Main Street"))
        self.assertFalse(numeric_street("I-80 Exit 21"))
        self.assertFalse(numeric_street("PO Box 24"))

    def test_facility_missing_city_requires_named_boundary_and_station_identity(self):
        row = {"names": ["River Truck Stop"], "City": "Hammond", "State": "LA"}
        places = {("LA", "hammond"): [Polygon([(-91, 30), (-90, 30), (-90, 31), (-91, 31)])]}
        record = {
            "name": "River Truck Stop",
            "street": "123 Main Street",
            "city": "",
            "state": "LA",
            "source_url": "https://example.com/facilities",
            "external_id": "LA123",
            "longitude": -90.5,
            "latitude": 30.5,
        }
        matches = list(epa_address_identities([record], {1: row}, places))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["city"], "Hammond")
        self.assertEqual(matches[0]["city_qualification"], "inside_named_census_place")
        self.assertNotIn("latitude", matches[0])
        self.assertNotIn("longitude", matches[0])
        for changes in (
            {"name": "Other Truck Stop"},
            {"longitude": -89.9},
            {"state": "MS"},
            {"latitude": None},
            {"latitude": float("nan")},
            {"street": "I-12 Exit 42"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(
                    list(epa_address_identities([{**record, **changes}], {1: row}, places))
                )
        self.assertFalse(list(epa_address_identities([record], {1: row}, {})))
        # An explicit source city is preserved rather than replaced by its GPS.
        explicit = list(epa_address_identities([{**record, "city": "Other"}], {1: row}, places))
        self.assertEqual(explicit[0]["city"], "Other")
        self.assertNotIn("city_qualification", explicit[0])

    def test_address_bridge_rejects_wrong_city_and_changed_retail_operator(self):
        row = {"names": ["Pilot #42"], "City": "Sterling", "State": "OK"}
        point = {
            "latitude": 35,
            "longitude": -97,
            "tags": {"name": "Pilot", "addr:city": "Sterling"},
        }
        self.assertEqual(len(street_matches(row, {}, [point], {})), 1)
        self.assertFalse(
            street_matches(
                row, {}, [{**point, "tags": {"name": "Pilot", "addr:city": "Edmond"}}], {}
            )
        )
        self.assertFalse(
            street_matches(
                row, {}, [{**point, "tags": {"name": "Love's", "addr:city": "Sterling"}}], {}
            )
        )
        self.assertFalse(street_matches(row, {}, [{**point, "tags": {"name": "Pilot"}}], {}))
        missing_city = {**point, "longitude": -96.99, "tags": {"name": "Pilot"}}
        search_anchors = {"sterling|OK": {"longitude": -97, "latitude": 35}}
        places = {
            ("OK", "sterling"): [Polygon([(-97.1, 34.9), (-97, 34.9), (-97, 35.1), (-97.1, 35.1)])]
        }
        self.assertFalse(street_matches(row, {}, [missing_city], search_anchors, places))

    def test_bulk_station_response_requires_complete_unique_records(self):
        with self.assertRaises(ValueError):
            station_records({"totalHits": 2, "docs": [{"id": "1"}]}, "https://example.com")
        with self.assertRaises(ValueError):
            station_records(
                {"totalHits": 2, "docs": [{"id": "1"}, {"id": "1"}]}, "https://example.com"
            )
        rows = station_records(
            {"totalHits": 1, "docs": [{"id": "1", "name": "Site", "phone": "private"}]},
            "https://example.com",
        )
        self.assertNotIn("phone", rows[0])
        self.assertEqual(rows[0]["source_url"], "https://example.com")

    def test_city_abbreviations_do_not_corrupt_unrelated_words(self):
        self.assertEqual(normalized("Fort Smith"), normalized("Ft. Smith"))
        self.assertEqual(normalized("Saint Louis"), normalized("St. Louis"))
        self.assertEqual(normalized("Comfort"), "comfort")
        self.assertEqual(normalized("Saints"), "saints")

    def test_store_number_recognizes_known_operator_titles_only(self):
        self.assertEqual(store_number("CASEY'S GENERAL STORE 4594 WAURIKA"), "4594")
        self.assertEqual(store_number("QUICK FUEL 2501 LOUISVILLE"), "2501")
        self.assertEqual(store_number("7-ELEVEN 40275"), "40275")
        self.assertIsNone(store_number("7-ELEVEN"))
        self.assertIsNone(store_number("US 50 TRUCK STOP"))

    def test_store_number_requires_matching_brand(self):
        row = {"names": ["PILOT #0042"]}
        self.assertEqual(
            identity(row, {"tags": {"brand": "Pilot", "ref": "42"}}), "same_brand_and_store_number"
        )
        self.assertIsNone(identity(row, {"tags": {"brand": "Love's", "ref": "42"}}))
        self.assertIsNone(identity(row, {"tags": {"brand": "Pilot", "ref": "43"}}))

    def test_generic_brand_name_cannot_identify_one_station(self):
        self.assertIsNone(
            identity({"names": ["Pilot Travel Center"]}, {"tags": {"name": "Pilot Travel Center"}})
        )
        self.assertIsNone(identity({"names": ["Circle K"]}, {"tags": {"name": "Circle K"}}))

    def test_distinctive_name_matches_punctuation_but_not_another_business(self):
        row = {"names": ["Woodshed of Big Cabin"]}
        self.assertEqual(
            identity(row, {"tags": {"name": "Woodshed Of Big Cabin"}}), "exact_station_name"
        )
        self.assertIsNone(identity(row, {"tags": {"name": "Woodshed of Vinita"}}))
        self.assertEqual(
            identity(
                {"names": ["New Jersey Truck Stop Diesel Only"], "City": "Kearny"},
                {"tags": {"name": "New Jersey Truck Stop", "addr:city": "Kearny"}},
            ),
            "exact_station_name",
        )

    def test_city_suffix_can_match_only_when_explicit_city_agrees(self):
        row = {"names": ["GALLOPS"], "City": "Michigan City"}
        self.assertEqual(
            identity(
                row, {"tags": {"name": "Gallops - Michigan City", "addr:city": "Michigan City"}}
            ),
            "exact_station_name",
        )
        self.assertIsNone(
            identity(row, {"tags": {"name": "Gallops - La Porte", "addr:city": "La Porte"}})
        )
        self.assertIsNone(identity(row, {"tags": {"name": "Gallops - Michigan City"}}))

    def test_short_business_name_needs_explicit_city_and_generic_brands_stay_excluded(self):
        row = {"names": ["TOPS TRUCK STOP"], "City": "Sterling"}
        self.assertEqual(
            identity(row, {"tags": {"name": "Tops", "addr:city": "Sterling"}}), "exact_station_name"
        )
        self.assertIsNone(identity(row, {"tags": {"name": "Tops"}}))
        self.assertIsNone(
            identity(
                {"names": ["Gulf"], "City": "Sterling"},
                {"tags": {"name": "Gulf", "addr:city": "Sterling"}},
            )
        )

    def test_exact_numbered_business_name_accepts_published_number_without_hash(self):
        self.assertEqual(
            identity(
                {"names": ["Sunoco #0053"], "City": "Sterling"},
                {"tags": {"name": "Sunoco 0053", "addr:city": "Sterling"}},
            ),
            "exact_station_name",
        )

    def test_wrong_city_is_rejected_even_when_station_name_matches(self):
        row = {"City": "Big Cabin", "State": "OK"}
        anchors = {"bigcabin|OK": {"longitude": -95.2246, "latitude": 36.534}}
        point = {"longitude": -97.5, "latitude": 35.5, "tags": {"addr:city": "Edmond"}}
        self.assertIsNone(same_place(row, point, anchors))
        point.update(longitude=-95.2247, latitude=36.5341)
        self.assertIsNone(same_place(row, point, anchors))
        point["tags"]["addr:city"] = ""
        self.assertEqual(same_place(row, point, anchors), "near_city_search_anchor")

    def test_missing_city_and_anchor_cannot_confirm_station_position(self):
        self.assertIsNone(
            same_place(
                {"City": "Unknown", "State": "OK"},
                {"longitude": -97, "latitude": 35, "tags": {}},
                {},
            )
        )

    def test_node_and_building_on_same_premises_are_deduplicated(self):
        nearby = [{"longitude": -97, "latitude": 35}, {"longitude": -97.0001, "latitude": 35.0001}]
        self.assertEqual(len(distinct_sites(nearby)), 1)
        self.assertEqual(len(distinct_sites(nearby + [{"longitude": -98, "latitude": 35}])), 2)

    def candidate(self, source, longitude=-97, availability="unknown"):
        return {
            "source": source,
            "longitude": longitude,
            "latitude": 35,
            "source_url": "https://example.com/42",
            "external_id": "42",
            "availability": availability,
        }

    def test_conflicting_source_points_require_review(self):
        selected, conflicts = reconcile(
            [
                self.candidate("pilot_official_locator"),
                self.candidate("catscale_official_location", -97.1),
            ]
        )
        self.assertIsNone(selected)
        self.assertGreater(conflicts[0]["distance_meters"], 600)

    def test_official_premises_preferred_and_closure_preserved(self):
        selected, conflicts = reconcile(
            [
                self.candidate("catscale_official_location", -97.0001, "closed"),
                self.candidate("pilot_official_locator"),
            ]
        )
        self.assertEqual(selected["source"], "pilot_official_locator")
        self.assertEqual(selected["operational_status"], "closed")
        self.assertFalse(conflicts)

    def test_manual_review_must_explain_each_rejected_conflicting_source(self):
        reviewed = self.candidate("manually_reviewed_station_premises")
        wrong = self.candidate("catscale_official_location", -97.1)
        wrong["source_url"] = "https://example.com/wrong-location"
        self.assertIsNone(reconcile([reviewed, wrong])[0])
        reviewed["rejected_candidates"] = [
            {
                "source_url": wrong["source_url"],
                "reason": "Named site and official street address agree elsewhere.",
            }
        ]
        selected, conflicts = reconcile([reviewed, wrong])
        self.assertFalse(conflicts)
        self.assertEqual(selected["source"], "manually_reviewed_station_premises")
        self.assertEqual(selected["corroborating_sources"], [])

    def test_shared_store_identity_requires_explicit_physical_alias_review(self):
        candidate = self.candidate("fuelman_public_station_locator")
        grouped = {1: [candidate], 2: [candidate]}
        self.assertEqual(set(identity_collisions(grouped, {})), {1, 2})
        self.assertFalse(identity_collisions(grouped, {1: 1, 2: 1}))
        other_brand = {
            **candidate,
            "source": "pilot_official_locator",
            "source_url": "https://another-brand.example/42",
        }
        self.assertFalse(identity_collisions({1: [candidate], 2: [other_brand]}, {}))

    def test_corpay_network_views_are_one_source_and_one_identity(self):
        first = self.candidate("fuelman_public_station_locator")
        second = {
            **first,
            "source": "cfn_public_station_locator",
            "source_url": "https://example.com/cfn",
        }
        selected, conflicts = reconcile([first, second])
        self.assertFalse(conflicts)
        self.assertEqual(selected["corroborating_sources"], [])
        self.assertEqual(set(identity_collisions({1: [first], 2: [second]}, {})), {1, 2})

    def test_review_rejects_only_identical_corpay_record_in_other_view(self):
        review = self.candidate("manually_reviewed_station_premises")
        wrong = self.candidate("fuelman_public_station_locator", -97.1)
        wrong["source_url"] = "https://example.com/fuelman"
        second = {
            **wrong,
            "source": "cfn_public_station_locator",
            "source_url": "https://example.com/cfn",
        }
        review["rejected_candidates"] = [
            {"source_url": wrong["source_url"], "reason": "Wrong premises."}
        ]
        selected, conflicts = reconcile([review, wrong, second])
        self.assertFalse(conflicts)
        self.assertEqual(len(selected["reviewed_rejections"]), 2)
        for modified in ({**second, "external_id": "43"}, {**second, "longitude": -97.2}):
            self.assertIsNone(reconcile([review, wrong, modified])[0])

    def test_reviewed_gps_rejection_does_not_discard_correct_point_or_another_record(self):
        review = self.candidate("manually_reviewed_station_premises")
        wrong = self.candidate("pilot_official_locator", -97.1)
        correct = self.candidate("pilot_official_locator", -97.0001, "closed")
        review["rejected_candidates"] = [
            {
                **wrong,
                "reason": "Incorrect published GPS; accepted source URL also establishes the station identity.",
            }
        ]
        selected, conflicts = reconcile([review, wrong, correct])
        self.assertFalse(conflicts)
        self.assertEqual(selected["operational_status"], "closed")
        self.assertEqual(len(selected["reviewed_rejections"]), 1)
        self.assertEqual(selected["source"], "manually_reviewed_station_premises")
        another = {**wrong, "external_id": "43"}
        self.assertIsNone(reconcile([review, wrong, another])[0])


class StationEligibilityTests(CachedTestCase):
    def test_confirmed_aliases_average_all_price_observations_once(self):
        create_station(1, price="3", site_id=1, price_observations=3)
        create_station(2, price="6", site_id=1, price_observations=1)
        status = dataset_status()
        stations = station_index(status["revision"]).stations
        self.assertEqual(status["us_stations"], 2)
        self.assertEqual(status["routable_stations"], 1)
        self.assertEqual(len(stations), 1)
        self.assertEqual(stations[0].opis_id, 1)
        self.assertEqual(stations[0].opis_aliases, (1, 2))
        self.assertEqual(stations[0].price, Decimal("3.75000000"))
        self.assertEqual(stations[0].price_observations, 4)

    def test_nearby_businesses_remain_separate_without_identity_evidence(self):
        create_station(1, price="3")
        create_station(2, price="6")
        status = dataset_status()
        stations = station_index(status["revision"]).stations
        self.assertEqual(len(stations), 2)
        self.assertEqual({station.price for station in stations}, {Decimal(3), Decimal(6)})

    def test_unverified_address_anchor_is_not_routable(self):
        create_station(coordinate_verified=False)
        status = dataset_status()
        self.assertEqual(status["us_stations"], 1)
        self.assertEqual(status["mapped_stations"], 0)
        self.assertEqual(len(station_index(status["revision"]).stations), 0)

    def test_known_closed_station_is_retained_but_not_routable(self):
        create_station(operational_status="closed")
        status = dataset_status()
        self.assertEqual(status["mapped_stations"], 1)
        self.assertEqual(status["known_closed_stations"], 1)
        self.assertEqual(status["routable_stations"], 0)
        self.assertEqual(len(station_index(status["revision"]).stations), 0)

    def test_status_change_invalidates_existing_spatial_index(self):
        create_station()
        old_revision = dataset_status()["revision"]
        old = station_index(old_revision)
        FuelStation.objects.update(operational_status="closed")
        new_revision = refresh_revision()
        self.assertNotEqual(old_revision, new_revision)
        self.assertEqual(len(old.stations), 1)
        self.assertEqual(len(station_index(new_revision).stations), 0)

    def test_verified_flag_requires_both_coordinates(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_station(latitude=None, longitude=None, coordinate_verified=True)

    def test_unknown_operational_status_is_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_station(operational_status="maybe")
