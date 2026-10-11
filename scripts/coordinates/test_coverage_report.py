"""Prevent duplicate coverage counts and reports based on mismatched artifacts."""

import unittest
from copy import deepcopy

from scripts.coordinates.coverage_report import (
    build_report,
    markdown,
    record_key,
    scripted_review_count,
    status_summary,
    validate_search_summary,
)


class CoverageReportTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {
                "OPIS Truckstop ID": str(identifier),
                "State": state,
                "Address": "100 Main Street",
                "City": "Example",
            }
            for identifier, state in ((1, "TX"), (1, "TX"), (2, "TX"), (3, "TX"), (4, "ON"))
        ]
        self.accepted = [self.point(1), self.point(2)]
        self.unresolved = [{"opis_id": 3}]
        self.conflicts = []
        self.exported = [self.export(point) for point in self.accepted]
        self.origins = {record_key(point): {"operator-candidates.json"} for point in self.accepted}
        self.baseline = {"accepted_ids": [1], "accepted_count": 1, "recorded_at": "baseline"}

    @staticmethod
    def point(identifier):
        return {
            "opis_id": identifier,
            "source": "loves_official_store",
            "source_url": "https://www.loves.com/locations/example",
            "external_id": str(identifier),
            "latitude": 30.0,
            "longitude": -95.0,
            "original_address": "100 Main Street",
            "original_city": "Example",
            "original_state": "TX",
            "operational_status": "unknown",
            "site_id": None,
        }

    @staticmethod
    def export(point):
        return {
            "opis_id": point["opis_id"],
            "source": point["source"],
            "latitude": str(point["latitude"]),
            "longitude": str(point["longitude"]),
            "address": point["original_address"],
            "city": point["original_city"],
            "state": point["original_state"],
            "verified": "true",
            "operational_status": point["operational_status"],
            "site_id": point["site_id"],
        }

    def report(self):
        return build_report(
            self.rows,
            {"TX"},
            self.accepted,
            self.unresolved,
            self.conflicts,
            self.exported,
            self.origins,
            self.baseline,
        )

    def test_observations_are_not_station_counts(self):
        counts = self.report()["counts"]
        self.assertEqual(counts["input_observations"], 5)
        self.assertEqual(counts["non_us_observations"], 1)
        self.assertEqual(counts["us_observations"], 4)
        self.assertEqual(counts["us_station_ids"], 3)
        self.assertEqual(counts["duplicate_us_observations"], 1)
        self.assertEqual(counts["accepted_station_ids"], 2)

    def test_method_partition_and_corroboration_overlap(self):
        self.accepted[0]["corroborating_sources"] = [
            {"source": "osm_station_poi"},
            {"source": "osm_station_poi"},
            {"source": "loves_official_store"},
        ]
        report = self.report()
        self.assertEqual(sum(method["accepted_count"] for method in report["methods"]), 2)
        self.assertEqual(
            report["retained_coordinate_source_counts_nonadditive"],
            {"loves_official_store": 2, "osm_station_poi": 1},
        )

    def test_closed_sites_and_physical_aliases_are_separate(self):
        self.accepted[1]["site_id"] = 1
        self.exported[1]["site_id"] = 1
        counts = self.report()["counts"]
        self.assertEqual(counts["routable_station_ids"], 2)
        self.assertEqual(counts["routable_distinct_sites"], 1)
        self.assertEqual(counts["extra_routable_alias_ids"], 1)
        self.accepted[1]["operational_status"] = "closed"
        self.exported[1]["operational_status"] = "closed"
        counts = self.report()["counts"]
        self.assertEqual(counts["status_excluded_station_ids"], 1)
        self.assertEqual(counts["routable_station_ids"], 1)
        self.assertEqual(counts["routable_distinct_sites"], 1)
        self.assertEqual(counts["extra_routable_alias_ids"], 0)

    def test_new_and_removed_are_not_conflated_with_net_change(self):
        self.baseline = {"accepted_ids": [1, 3], "accepted_count": 2}
        report = self.report()
        self.assertEqual(report["run"]["new_accepted_ids"], [2])
        self.assertEqual(report["run"]["removed_accepted_ids"], [3])
        self.assertEqual(report["run"]["net_new_accepted_count"], 0)
        self.assertEqual(sum(method["new_accepted_count"] for method in report["methods"]), 1)

    def test_partition_rejects_missing_unknown_and_overlapping_ids(self):
        for pending in ([], [{"opis_id": 4}], [{"opis_id": 1}, {"opis_id": 3}]):
            with self.subTest(pending=pending):
                self.unresolved = pending
                with self.assertRaisesRegex(ValueError, "partition"):
                    self.report()

    def test_duplicate_ids_in_any_partition_or_export_fail(self):
        for field in ("accepted", "unresolved", "exported"):
            with self.subTest(field=field):
                original = deepcopy(getattr(self, field))
                getattr(self, field).append(deepcopy(original[0]))
                with self.assertRaisesRegex(ValueError, "Duplicate"):
                    self.report()
                setattr(self, field, original)

    def test_conflicts_are_an_exclusive_partition(self):
        self.conflicts = self.unresolved
        self.unresolved = []
        report = self.report()
        self.assertEqual(report["counts"]["coordinate_conflict_station_ids"], 1)
        self.assertEqual(report["counts"]["unresolved_station_ids"], 0)

    def test_unknown_method_requires_documentation(self):
        self.accepted[0]["source"] = "unsupported_new_provider"
        with self.assertRaisesRegex(ValueError, "explicit explanation"):
            self.report()

    def test_export_must_match_coordinates_identity_and_status(self):
        for column, value in (
            ("latitude", "30.1"),
            ("source", "osm_station_poi"),
            ("verified", "false"),
            ("address", "101 Main Street"),
            ("city", "Other"),
            ("state", "OK"),
            ("operational_status", "closed"),
            ("site_id", 2),
        ):
            with self.subTest(column=column):
                original = self.exported[0][column]
                self.exported[0][column] = value
                with self.assertRaisesRegex(ValueError, "differs"):
                    self.report()
                self.exported[0][column] = original

    def test_export_missing_id_fails(self):
        self.exported.pop()
        with self.assertRaisesRegex(ValueError, "station IDs differ"):
            self.report()

    def test_selected_source_must_have_exact_candidate_provenance(self):
        self.origins.clear()
        with self.assertRaisesRegex(ValueError, "no matching candidate"):
            self.report()

    def test_source_address_changes_make_the_report_fail(self):
        self.rows[2]["Address"] = "200 Other Street"
        with self.assertRaisesRegex(ValueError, "stale source-address binding"):
            self.report()

    def test_repeated_input_files_do_not_increase_station_count(self):
        self.origins[record_key(self.accepted[0])].add("other-candidates.json")
        report = self.report()
        self.assertEqual(report["counts"]["accepted_station_ids"], 2)
        self.assertEqual(
            report["selected_input_files_nonadditive"],
            {"operator-candidates.json": 2, "other-candidates.json": 1},
        )

    def test_baseline_rejects_wrong_count_duplicate_and_unknown_ids(self):
        for baseline in (
            {"accepted_ids": [1], "accepted_count": 2},
            {"accepted_ids": [1, 1], "accepted_count": 2},
            {"accepted_ids": [4], "accepted_count": 1},
        ):
            with self.subTest(baseline=baseline):
                self.baseline = baseline
                with self.assertRaises(ValueError):
                    self.report()

    def test_tomtom_attribution_requires_unique_final_accepted_ids(self):
        for identifiers in ([1, 1], [3]):
            with (
                self.subTest(identifiers=identifiers),
                self.assertRaisesRegex(ValueError, "unique final accepted"),
            ):
                validate_search_summary(
                    {"independently_accepted_ids": identifiers, "response_retained": False}, {1}
                )
        validate_search_summary(
            {"independently_accepted_ids": [1], "response_retained": False}, {1}
        )

    def test_tomtom_report_cannot_claim_responses_were_unretained_when_retained(self):
        with self.assertRaisesRegex(ValueError, "unretained"):
            validate_search_summary(
                {"independently_accepted_ids": [1], "response_retained": True}, {1}
            )

    def test_status_reviews_must_match_excluded_ids_exactly(self):
        review = {"opis_id": 1, "category": "maps_temporary"}
        for records, excluded in (([], [1]), ([review], []), ([review, review], [1])):
            with self.subTest(records=records, excluded=excluded), self.assertRaises(ValueError):
                status_summary(records, excluded)
        with self.assertRaisesRegex(ValueError, "Unknown"):
            status_summary([{"opis_id": 1, "category": "unsupported"}], [1])

    def test_temporary_and_permanent_status_reviews_remain_distinct(self):
        groups = status_summary(
            [
                {"opis_id": 1, "category": "maps_temporary"},
                {"opis_id": 2, "category": "maps_permanent"},
            ],
            [1, 2],
        )
        indexed_groups = {item["category"]: item for item in groups}
        self.assertEqual(indexed_groups["maps_temporary"]["opis_ids"], [1])
        self.assertEqual(indexed_groups["maps_permanent"]["opis_ids"], [2])
        self.assertEqual(sum(group["count"] for group in groups), 2)

    def test_scripted_review_minimum_does_not_classify_every_review_as_manual(self):
        scripted = {
            "source": "manually_reviewed_station_premises",
            "evidence": [
                {"reason": "Official chain record matches station identity and street address: A"},
                {"reason": "Independent named fuel premises (30, -95) agree."},
            ],
        }
        individual = {
            "source": "manually_reviewed_station_premises",
            "evidence": [{"reason": "Individual canopy inspection."}],
        }
        self.assertEqual(scripted_review_count([scripted, individual]), 1)
        scripted["source"] = "loves_official_store"
        self.assertEqual(scripted_review_count([scripted, individual]), 0)
        individual["evidence"] = ["unstructured evidence", "another item"]
        self.assertEqual(scripted_review_count([individual]), 0)

    def document_report(self):
        self.accepted[1]["operational_status"] = "closed"
        self.exported[1]["operational_status"] = "closed"
        report = self.report()
        report["generated_at"] = "2026-10-12T00:00:00Z"
        report["census"] = {"initial_exact_address_anchors": 75}
        report["scripted_review_minimum"] = 0
        report["status_exclusion_groups"] = status_summary(
            [{"opis_id": 2, "category": "maps_temporary"}], [2]
        )
        report["tomtom_search"] = {
            "pilot_http_requests": 7,
            "search_request_count": 10,
            "searched_station_count": 9,
            "independently_accepted_ids": [2],
            "independent_audit_only_accepted_ids": [1],
            "remaining_context_reasons": {"no_independent_branch": 1},
        }
        return report

    def test_public_report_separates_observations_coordinates_exclusions_and_searches(self):
        document = markdown(self.document_report())
        self.assertIn("| Input price observations | 5 |", document)
        self.assertIn("| Unique US station IDs | 3 |", document)
        self.assertIn("| Mapped station IDs | 2 |", document)
        self.assertIn("| Mapped IDs excluded by status | 1 |", document)
        self.assertIn("| Maps temporary-closure label | 1 |", document)
        self.assertIn("| No exact independent branch identity | 1 |", document)
        self.assertIn("1 of 1 eligible IDs have opening status recorded as unknown", document)
        self.assertIn("TomTom made 17 HTTP requests", document)
        self.assertIn("covering 9 IDs", document)
        self.assertIn("It assisted 1 accepted matches", document)
        self.assertIn("a separate audit accepted 1 more", document)
        self.assertIn("already counted above", document)
        self.assertNotIn("Manual premises and coordinate review", document)
        self.assertNotIn("Newly accepted in this run", document)
        self.assertNotIn("### Retained coordinate evidence", document)

    def test_public_report_rejects_stale_unresolved_reason_counts(self):
        report = self.document_report()
        report["tomtom_search"]["remaining_context_reasons"]["no_independent_branch"] = 2
        with self.assertRaisesRegex(ValueError, "unresolved total"):
            markdown(report)


if __name__ == "__main__":
    unittest.main()
