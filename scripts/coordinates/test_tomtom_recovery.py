import copy
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from shapely.geometry import box

from scripts.coordinates.tomtom import recover_independent
from scripts.coordinates.tomtom_recovery import build_contexts, crosscheck_independent


class IndependentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        (self.data / "osm-states").mkdir()
        self.original = {
            "opis_id": 101,
            "names": ["Oak Travel Plaza #15"],
            "address": "I-5, EXIT 72",
            "city": "Chehalis",
            "state": "WA",
        }
        self.source = {
            "name": "Oak Travel Plaza #15",
            "address": "1366 Rush Rd",
            "city": "Chehalis",
            "state": "WA",
            "latitude": 46.6,
            "longitude": -122.9,
            "source_url": "https://example.org/public/15",
            "external_id": "15",
        }
        self.point = {
            "type": "node",
            "id": 77,
            "latitude": 46.6001,
            "longitude": -122.9,
            "tags": {"amenity": "fuel", "name": "Oak Travel Plaza"},
        }
        self.provider = {
            "id": "ephemeral-do-not-retain",
            "type": "poi",
            "title": "Oak Travel Plaza #15",
            "position": {"type": "Point", "coordinates": [-122.9, 46.6]},
            "poiTypes": [{"id": "fuel_station"}],
            "address": {
                "countryCodeIso2": "US",
                "countrySubdivisionCodeIso": "US-WA",
                "municipality": "Chehalis",
                "houseNumber": "1366",
                "street": "Rush Road",
            },
        }
        self.boundaries = {"WA": box(-125, 45, -116, 50)}
        self.requests = []
        self.candidates_path = self.data / "tomtom-independent-candidates.json"

    def write(self, name, payload):
        (self.data / name).write_text(json.dumps(payload), encoding="utf-8")

    def context(self, sources=None, points=None):
        self.write("unresolved.json", [self.original])
        self.write(
            "independent-identity-proposals.json",
            [
                {
                    "opis_id": 101,
                    "possible": [
                        {"source": item}
                        for item in (sources if sources is not None else [self.source])
                    ],
                }
            ],
        )
        self.write(
            "osm-states/washington.json",
            {"records": points if points is not None else [self.point]},
        )
        return build_contexts(self.data)[101]

    def recover(self, contexts, pending_ids):
        def handler(request):
            self.requests.append(request)
            return httpx.Response(200, json={"results": [self.provider]})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return recover_independent(
                contexts,
                {"chehalis|WA": {"longitude": -122.9, "latitude": 46.6}},
                self.boundaries,
                "private-test-secret",
                client=client,
                crosscheck=crosscheck_independent,
                journal_path=self.data / "journal.json",
                candidates_path=self.candidates_path,
                pending_ids=pending_ids,
            )

    def test_returned_coordinates_and_evidence_are_entirely_independent(self):
        context = self.context()
        result = crosscheck_independent(context, {"results": [self.provider]}, self.boundaries)
        self.assertEqual(result["latitude"], self.point["latitude"])
        self.assertNotEqual(result["latitude"], self.provider["position"]["coordinates"][1])
        self.assertEqual(result["external_id"], "osm:node:77")
        self.assertEqual(result["source"], "mapped_fuel_with_published_street_identity")
        self.assertNotIn("ephemeral-do-not-retain", json.dumps(result))
        self.assertNotIn("TomTom", json.dumps(result))
        result["evidence"].clear()
        self.assertTrue(context["candidate"]["evidence"])

    def test_wrong_or_missing_independent_store_number_is_not_enriched(self):
        for name in ("Oak Travel Plaza #99", "Oak Travel Plaza"):
            with self.subTest(name=name):
                self.assertNotIn(
                    "known_address", self.context([{**self.source, "name": name}])["row"]
                )

    def test_prefix_and_suffix_collisions_are_rejected(self):
        for name in ("Oak Travel Plaza #150", "Oak Travel Plaza #115"):
            with self.subTest(name=name):
                self.assertIsNone(self.context([{**self.source, "name": name}])["candidate"])

    def test_competing_numeric_streets_are_not_selected_by_nearness(self):
        context = self.context([self.source, {**self.source, "address": "1368 Rush Rd"}])
        self.assertEqual(context["reason"], "ambiguous_independent_streets")
        self.assertNotIn("known_address", context["row"])

    def test_wrong_city_or_state_does_not_enrich(self):
        for updates in ({"city": "Centralia"}, {"state": "OR"}):
            with self.subTest(updates=updates):
                self.assertIsNone(self.context([{**self.source, **updates}])["candidate"])

    def test_generic_brands_need_original_numeric_street(self):
        for name in ("Shell", "Mobil", "BP", "Chevron"):
            self.original["names"] = [name]
            self.source["name"] = name
            with self.subTest(name=name):
                self.assertEqual(self.context()["reason"], "generic_brand_without_original_street")
        self.original["address"] = "1366 Rush Road"
        self.point["tags"]["name"] = "Chevron"
        self.assertIsNotNone(self.context()["candidate"])

    def test_city_only_name_requires_original_street_or_branch_number(self):
        self.original.update(names=["Atoka Fuel"], city="Atoka")
        self.source.update(name="Atoka Travel Plaza", city="Atoka")
        self.point["tags"]["name"] = "Shell"
        self.assertEqual(self.context()["reason"], "city_only_name_without_original_street")

        self.original["address"] = "1366 Rush Rd"
        self.assertIsNotNone(self.context()["candidate"])

        self.original.update(names=["Atoka Fuel #15"], address="I-5, EXIT 72")
        self.source["name"] = "Atoka Travel Plaza #15"
        self.assertIsNotNone(self.context()["candidate"])

    def test_run_merge_run_revalidates_and_retains_source_without_new_requests(self):
        context = self.context()
        self.assertEqual(self.recover({101: context}, {101})["independent_candidates_added"], 1)
        saved = json.loads(self.candidates_path.read_text())
        self.write("merged.json", saved)
        self.write("unresolved.json", [])

        contexts = build_contexts(self.data, {101}, original_rows=[self.original])
        outcome = self.recover(contexts, set())

        self.assertEqual(outcome["requests_this_run"], 0)
        self.assertEqual(outcome["independent_candidates_total"], 1)
        self.assertEqual(json.loads(self.candidates_path.read_text()), saved)
        self.assertEqual(len(self.requests), 1)

    def test_previously_merged_source_is_removed_when_current_original_or_hold_changes(self):
        context = self.context()
        self.recover({101: context}, {101})
        saved = json.loads(self.candidates_path.read_text())
        self.write("merged.json", saved)
        self.write("unresolved.json", [])

        changed = {**self.original, "names": ["Oak Travel Plaza #99"]}
        contexts = build_contexts(self.data, {101}, original_rows=[changed])
        outcome = self.recover(contexts, set())
        self.assertEqual(outcome["requests_this_run"], 0)
        self.assertEqual(json.loads(self.candidates_path.read_text()), [])

        self.write("tomtom-independent-candidates.json", saved)
        self.write("root-test-held-identities.json", [{"opis_id": 101}])
        contexts = build_contexts(self.data, {101}, original_rows=[self.original])
        outcome = self.recover(contexts, set())
        self.assertEqual(outcome["requests_this_run"], 0)
        self.assertEqual(json.loads(self.candidates_path.read_text()), [])
        self.assertEqual(len(self.requests), 1)

    def test_original_override_keeps_current_pending_identity_conflict(self):
        self.context()
        self.write("unresolved.json", [{**self.original, "identity_collisions": [202]}])
        contexts = build_contexts(self.data, {101}, original_rows=[self.original])
        self.assertEqual(contexts[101]["reason"], "held_identity")

    def test_original_numeric_street_conflict_cannot_be_overwritten(self):
        self.original["address"] = "1368 Rush Rd"
        context = self.context()
        self.assertEqual(context["reason"], "original_street_conflict")
        self.assertNotIn("known_address", context["row"])

    def test_previous_held_identity_cannot_be_resolved_by_search(self):
        self.write("root-test-held-identities.json", [{"opis_id": 101}])
        context = self.context()
        self.assertEqual(context["reason"], "held_identity")
        self.assertIsNone(
            crosscheck_independent(context, {"results": [self.provider]}, self.boundaries)
        )

    def test_missing_actual_fuel_is_not_promoted_but_known_street_is_available(self):
        for point in (
            {**self.point, "tags": {"shop": "convenience"}},
            {**self.point, "latitude": 46.61},
        ):
            with self.subTest(point=point):
                context = self.context(points=[point])
                self.assertIsNone(context["candidate"])
                self.assertEqual(context["row"]["known_address"], self.source["address"])

    def test_conflicting_complete_mapped_address_is_not_promoted(self):
        point = {
            **self.point,
            "tags": {**self.point["tags"], "addr:housenumber": "1368", "addr:street": "Rush Rd"},
        }
        self.assertIsNone(self.context(points=[point])["candidate"])

    def test_displaced_independent_native_pin_is_not_promoted(self):
        context = self.context([{**self.source, "latitude": 46.61}])
        self.assertIsNone(context["candidate"])

    def test_adjacent_unlabelled_fuel_properties_are_ambiguous_even_within_100m(self):
        points = [
            self.point,
            {**self.point, "id": 78, "latitude": 46.6002, "tags": {"amenity": "fuel"}},
        ]
        self.assertEqual(self.context(points=points)["reason"], "multiple_mapped_fuel_properties")

    def test_distinct_mapped_name_or_brand_conflict_is_rejected(self):
        for tags in (
            {"amenity": "fuel", "name": "Maple Travel Plaza"},
            {"amenity": "fuel", "name": "Maple Shell"},
        ):
            with self.subTest(tags=tags):
                self.assertIsNone(self.context(points=[{**self.point, "tags": tags}])["candidate"])
        self.original["names"] = ["Chevron #15"]
        self.source["name"] = "Chevron #15"
        self.assertIsNone(
            self.context(points=[{**self.point, "tags": {"amenity": "fuel", "name": "Shell"}}])[
                "candidate"
            ]
        )

    def test_existing_station_binding_does_not_create_an_alias(self):
        self.write(
            "merged.json", [{"opis_id": 202, "source_url": "https://www.openstreetmap.org/node/77"}]
        )
        self.assertEqual(self.context()["reason"], "mapped_fuel_bound_to_another_station")

    def test_contradictory_mapped_name_and_brand_are_not_hidden_by_generic_name(self):
        point = {**self.point, "tags": {"amenity": "fuel", "brand": "Chevron", "name": "Shell"}}
        self.assertIsNone(self.context(points=[point])["candidate"])

        self.original["names"] = ["Chevron #15"]
        self.source["name"] = "Chevron #15"
        self.assertIsNone(self.context(points=[point])["candidate"])

        point["tags"]["brand"] = "Unrecognized local brand"
        self.assertIsNone(self.context(points=[point])["candidate"])

    def test_distinctive_matching_name_can_coexist_with_a_fuel_brand(self):
        point = {
            **self.point,
            "tags": {**self.point["tags"], "brand": "Chevron"},
        }
        self.assertIsNotNone(self.context(points=[point])["candidate"])

    def test_bad_numeric_coordinates_are_rejected(self):
        for updates in ({"latitude": True}, {"longitude": float("nan")}, {"latitude": 0}):
            with self.subTest(updates=updates):
                self.assertIsNone(self.context([{**self.source, **updates}])["candidate"])

    def test_independent_coordinate_must_itself_be_inside_original_state(self):
        context = self.context()
        boundaries = {"WA": box(-125, 45, -116, 46.60005)}
        self.assertIsNone(crosscheck_independent(context, {"results": [self.provider]}, boundaries))

    def test_transient_wrong_branch_displacement_and_ambiguity_are_rejected(self):
        context = self.context()
        other = copy.deepcopy(self.provider)
        other["id"] = "other"
        other["position"]["coordinates"][1] = 46.6002
        for payload in (
            {"results": [{**self.provider, "title": "Oak Travel Plaza #99"}]},
            {
                "results": [
                    {**self.provider, "position": {"type": "Point", "coordinates": [-122.9, 46.61]}}
                ]
            },
            {"results": [self.provider, other]},
            {},
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(crosscheck_independent(context, payload, self.boundaries))


if __name__ == "__main__":
    unittest.main()
