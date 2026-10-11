import json
import math
import tempfile
import unittest
from pathlib import Path

import httpx
from shapely.geometry import box

from scripts.coordinates.tomtom import (
    ATTRIBUTES,
    ENDPOINT,
    assess_response,
    build_queue,
    identity_rule,
    recover_independent,
    run_queue,
    search_body,
    search_name,
)


def row(**updates):
    return {
        "opis_id": 101,
        "names": ["Chevron #382354"],
        "address": "I-5, EXIT 72",
        "city": "Chehalis",
        "state": "WA",
        **updates,
    }


def result(**updates):
    return {
        "id": "place-123",
        "type": "poi",
        "title": "Chevron #382354",
        "position": {"type": "Point", "coordinates": [-122.9, 46.6]},
        "poiTypes": [{"id": "fuel_station"}],
        "address": {
            "countryCodeIso2": "US",
            "countrySubdivisionCodeIso": "US-WA",
            "municipality": "Chehalis",
            "houseNumber": "1366",
            "street": "Rush Road",
        },
        "contacts": [{"phones": ["+1 360 555 1234"]}],
        **updates,
    }


BOUNDARIES = {"WA": box(-125, 45, -116, 50)}


class MatchTests(unittest.TestCase):
    def assess(self, original=None, results=None):
        return assess_response(original or row(), {"results": results or [result()]}, BOUNDARIES)

    def test_exact_number_brand_city_state_fuel_point_is_unverified_proposal(self):
        decision = self.assess()
        self.assertEqual(decision["status"], "unique_branch_proposal")
        proposal = decision["proposals"][0]
        self.assertEqual(proposal["identity_rule"], "exact_brand_store_number_city_state")
        self.assertFalse(proposal["coordinate_verified"])
        self.assertEqual((proposal["longitude"], proposal["latitude"]), (-122.9, 46.6))

    def test_wrong_store_number_cannot_fall_back_to_same_street_or_phone(self):
        original = row(address="1366 Rush Rd", known_phones=["3605551234"])
        self.assertIsNone(identity_rule(original, result(title="Chevron #99")))

    def test_exact_numeric_street_with_brand_supports_unnumbered_branch(self):
        decision = self.assess(row(names=["Chevron"], address="1366 Rush Rd"))
        self.assertEqual(decision["status"], "unique_branch_proposal")
        self.assertEqual(
            decision["proposals"][0]["identity_rule"],
            "exact_name_or_brand_numeric_street_city_state",
        )

    def test_wrong_street_number_direction_or_brand_rejected(self):
        for original in (
            row(names=["Chevron"], address="1368 Rush Rd"),
            row(names=["Chevron"], address="1366 N Rush Rd"),
            row(names=["Shell"], address="1366 Rush Rd"),
        ):
            with self.subTest(original=original):
                self.assertEqual(self.assess(original)["status"], "unresolved")

    def test_bare_brand_or_exact_distinctive_name_alone_cannot_establish_branch(self):
        for original, title in (
            (row(names=["Chevron"]), "Chevron"),
            (row(names=["Blue Mountain Travel Plaza"]), "Blue Mountain Travel Plaza"),
        ):
            with self.subTest(title=title):
                self.assertIsNone(identity_rule(original, result(title=title)))

    def test_branch_phone_uses_complete_us_phone_not_substring(self):
        original = row(names=["Chevron"], known_phones=["(360) 555-1234"])
        self.assertEqual(identity_rule(original, result()), "exact_name_or_brand_phone_city_state")
        for phones in (["5551234"], ["3605551235"], ["136055512345"]):
            self.assertIsNone(identity_rule({**original, "known_phones": phones}, result()))

    def test_exact_operator_street_is_an_optional_independent_identity_bridge(self):
        original = row(names=["Chevron"], known_address="1366 Rush Rd")
        self.assertIsNotNone(identity_rule(original, result()))

    def test_country_state_city_and_physical_state_boundary_are_required(self):
        for updates in (
            {"countryCodeIso2": "CA"},
            {"countrySubdivisionCodeIso": "US-OR"},
            {"municipality": "Centralia"},
            {"municipality": ""},
        ):
            actual = result()
            actual["address"].update(updates)
            with self.subTest(updates=updates):
                self.assertEqual(self.assess(results=[actual])["status"], "unresolved")
        actual = result(position={"type": "Point", "coordinates": [-80, 30]})
        self.assertEqual(self.assess(results=[actual])["status"], "unresolved")
        self.assertEqual(
            assess_response(row(), {"results": [result()]}, {})["status"], "unresolved"
        )

    def test_centroid_address_shop_ev_or_missing_station_id_excluded(self):
        for updates in (
            {"type": "area"},
            {"type": "address"},
            {"poiTypes": [{"id": "convenience_store"}]},
            {"poiTypes": [{"id": "charging_location"}]},
            {"id": ""},
        ):
            with self.subTest(updates=updates):
                self.assertEqual(self.assess(results=[result(**updates)])["status"], "unresolved")

    def test_missing_invalid_nonfinite_or_reversed_coordinate_rejected(self):
        for position in (
            {},
            {"type": "Point", "coordinates": [math.nan, 46]},
            {"type": "Point", "coordinates": [-122, math.inf]},
            {"type": "Point", "coordinates": [-190, 46]},
            {"type": "Point", "coordinates": [-122, 91]},
            {"type": "Point", "coordinates": [False, 46]},
            {"type": "Point", "coordinates": [-122]},
            {"type": "LineString", "coordinates": [-122.9, 46.6]},
            {"type": "Point", "coordinates": [46.6, -122.9]},
        ):
            with self.subTest(position=position):
                self.assertEqual(
                    self.assess(results=[result(position=position)])["status"], "unresolved"
                )

    def test_separate_matching_places_remain_ambiguous_without_score_ranking(self):
        decision = self.assess(results=[result(), result(id="another", title="Chevron #382354")])
        self.assertEqual(decision["status"], "ambiguous")
        self.assertEqual(len(decision["proposals"]), 2)

    def test_identical_repeated_result_collapses_but_conflicting_same_id_does_not(self):
        self.assertEqual(
            self.assess(results=[result(), result()])["status"], "unique_branch_proposal"
        )
        different = result(position={"type": "Point", "coordinates": [-122.89, 46.6]})
        self.assertEqual(self.assess(results=[result(), different])["status"], "ambiguous")

    def test_prior_identity_conflicts_are_never_silently_overridden(self):
        for updates in (
            {"identity_review": {"reason": "two branches"}},
            {"identity_collisions": [2]},
        ):
            self.assertEqual(self.assess(row(**updates))["status"], "unresolved")

    def test_malformed_collection_fails_instead_of_empty_success(self):
        for payload in ([], {"unexpected": True}, {"results": {}}, {"results": None}):
            with self.assertRaises(TypeError):
                assess_response(row(), payload, BOUNDARIES)

    def test_malformed_nested_provider_fields_are_rejected_without_crashing(self):
        for updates in (
            {"address": []},
            {"address": {"municipality": 7}},
            {"position": "point"},
            {"poiTypes": "fuel_station"},
            {"poiTypes": ["fuel_station"]},
            {"contacts": [{"phones": "3605551234"}]},
            {"contacts": ["phone"]},
            {"contacts": [{"phones": [1234]}]},
            {"id": ["place"]},
        ):
            with self.subTest(updates=updates):
                self.assertEqual(self.assess(results=[result(**updates)])["status"], "unresolved")


class QueueTests(unittest.TestCase):
    def test_query_is_country_and_fuel_constrained_with_lon_lat_anchor(self):
        body = search_body(row(), {"longitude": -122.9, "latitude": 46.6})
        self.assertEqual(body["filters"]["countryCodesIso2"], ["US"])
        self.assertEqual(body["filters"]["poiTypes"], ["fuel_station"])
        self.assertEqual(body["preferences"]["geometry"]["coordinates"], [-122.9, 46.6])
        self.assertNotIn("geopoliticalView", body)
        self.assertNotIn("EXIT", body["query"])

    def test_numeric_street_and_operator_bridge_used_but_highway_description_is_not_address(self):
        self.assertEqual(
            "1366 Rush Rd, Chehalis, WA",
            search_body(row(address="1366 Rush Rd"), query_mode="address")["query"],
        )
        self.assertEqual(
            "1366 Rush Rd, Chehalis, WA",
            search_body(row(known_address="1366 Rush Rd"), query_mode="address")["query"],
        )
        self.assertNotIn("EXIT", search_body(row())["query"])

    def test_name_query_removes_nonunique_descriptors_and_store_number_only_for_search(self):
        self.assertEqual(search_name("Love's Travel Stop #0012"), "Love's")
        self.assertEqual(search_name("BLUE MOUNTAIN TRUCK STOP"), "BLUE MOUNTAIN")
        original = row(names=["Chevron #382354"])
        self.assertEqual(search_body(original)["query"], "Chevron, Chehalis, WA")
        self.assertEqual(original["names"], ["Chevron #382354"])
        with self.assertRaises(ValueError):
            search_body(original, query_mode="address")
        with self.assertRaises(ValueError):
            search_body(original, query_mode="unknown")

    def test_queue_digest_stable_but_changes_for_actual_query_change(self):
        first = build_queue([row()])[0]["request_id"]
        self.assertEqual(first, build_queue([row()])[0]["request_id"])
        self.assertNotEqual(first, build_queue([row(names=["Shell #99"])])[0]["request_id"])

    def test_invalid_row_duplicate_id_and_anchor_fail_before_network(self):
        for updates in ({"opis_id": True}, {"state": "ON"}, {"city": ""}, {"names": []}):
            with self.assertRaises(ValueError):
                build_queue([row(**updates)])
        with self.assertRaises(ValueError):
            build_queue([row(), row()])
        with self.assertRaises(ValueError):
            search_body(row(), {"longitude": math.nan, "latitude": 46})


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.journal = Path(self.folder.name) / "journal.json"
        self.now = 0
        self.sleeps = []
        self.requests = []
        self.payloads = []
        self.queue = build_queue([row(), row(opis_id=102), row(opis_id=103)])

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.sleeps.append(seconds)

    def handler(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"results": [result()]})

    def review(self, entry, payload):
        self.payloads.append(payload)
        return "unique_branch_proposal"

    def run_search(self, handler=None, **options):
        with httpx.Client(
            transport=httpx.MockTransport(handler or self.handler), timeout=30
        ) as client:
            return run_queue(
                self.queue,
                self.journal,
                "private-test-secret",
                client=client,
                handle_result=self.review,
                clock=self.clock,
                sleep=self.sleep,
                **options,
            )

    def test_lifetime_budget_resume_and_journal_do_not_persist_provider_content_or_key(self):
        first = self.run_search(max_requests=1)
        self.assertEqual(
            first, {"requests_this_run": 1, "requests_total": 1, "stop": "request_budget"}
        )
        self.assertEqual(self.run_search(max_requests=2)["requests_this_run"], 1)
        self.assertEqual(self.run_search(max_requests=2)["requests_this_run"], 0)
        self.assertEqual(len(self.requests), 2)
        text = self.journal.read_text()
        self.assertNotIn("private-test-secret", text)
        self.assertNotIn("place-123", text)
        self.assertNotIn("Rush Road", text)
        self.assertNotIn("360 555", text)
        self.assertNotIn("position", text)
        self.assertIn(
            '"query": "Chevron, Chehalis, WA"', text
        )  # Our caller-supplied query is retained.
        self.assertEqual(self.requests[0].url, ENDPOINT)
        self.assertEqual(self.requests[0].headers["TomTom-Api-Version"], "3")
        self.assertEqual(self.requests[0].headers["Attributes"], ATTRIBUTES)

    def test_serial_requests_spaced_at_least_one_second(self):
        self.assertEqual(self.run_search()["requests_this_run"], 3)
        self.assertEqual(self.sleeps, [1, 1])

    def test_deadline_does_not_start_request_after_throttle_would_exceed_budget(self):
        self.assertEqual(self.run_search(max_seconds=1)["stop"], "time_budget")
        self.assertEqual(len(self.requests), 1)

    def test_each_http_error_halts_without_retries_or_response_body_logging(self):
        for status in (400, 401, 403, 404, 429, 500, 503):
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory() as folder,
            ):
                self.journal = Path(folder) / "journal.json"
                self.requests.clear()

                def failure(request, status=status):
                    self.requests.append(request)
                    return httpx.Response(status, text="private-test-secret returned in error body")

                outcome = self.run_search(failure)
                self.assertEqual(outcome["stop"], f"http_{status}")
                self.assertEqual(outcome["requests_this_run"], 1)
                self.assertEqual(self.run_search(failure)["requests_this_run"], 0)
                self.assertEqual(len(self.requests), 1)
                self.assertNotIn("private-test-secret", self.journal.read_text())

    def test_timeout_stops_and_redacts_exception_text(self):
        def timeout(request):
            raise httpx.ReadTimeout("private-test-secret", request=request)

        self.assertEqual(self.run_search(timeout)["stop"], "transport_error")
        self.assertNotIn("private-test-secret", self.journal.read_text())

    def test_invalid_json_and_missing_results_halt(self):
        for response in (
            httpx.Response(200, text="not-json"),
            httpx.Response(200, json={"unexpected": True}),
        ):
            with tempfile.TemporaryDirectory() as folder:
                self.journal = Path(folder) / "journal.json"
                outcome = self.run_search(lambda request, response=response: response)
                self.assertEqual(outcome["stop"], "invalid_response_or_handler")
                self.assertEqual(outcome["requests_this_run"], 1)

    def test_empty_discover_object_is_completed_zero_result_search(self):
        outcome = self.run_search(lambda request: httpx.Response(200, json={}))
        self.assertEqual(outcome["stop"], "complete")
        self.assertEqual(outcome["requests_this_run"], 3)
        journal = json.loads(self.journal.read_text())
        self.assertTrue(all(attempt["result_count"] == 0 for attempt in journal["attempts"]))

    def test_attempt_is_journaled_before_http_and_handler_output_cannot_leak_content(self):
        def success(request):
            journal = json.loads(self.journal.read_text())
            self.assertEqual(journal["attempts"][-1]["status"], "started")
            return httpx.Response(200, json={"results": []})

        self.review = lambda entry, payload: "private-test-secret"
        self.assertEqual(self.run_search(success)["stop"], "complete")
        self.assertNotIn("private-test-secret", self.journal.read_text())

    def test_interrupted_inflight_attempt_is_counted_and_not_automatically_repeated(self):
        self.journal.write_text(
            json.dumps(
                {
                    "version": 1,
                    "halted": None,
                    "attempts": [{**self.queue[0], "status": "started"}],
                }
            )
        )
        self.assertEqual(self.run_search(max_requests=2)["requests_this_run"], 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(json.loads(self.requests[0].content), self.queue[1]["body"])

    def test_missing_key_and_invalid_limits_fail_without_network(self):
        for options in (
            {"max_requests": 0},
            {"max_requests": 5001},
            {"interval_seconds": 0.1},
            {"max_seconds": math.nan},
            {"max_seconds": -1},
        ):
            with self.assertRaises(ValueError):
                self.run_search(**options)
        with (
            httpx.Client(transport=httpx.MockTransport(self.handler)) as client,
            self.assertRaises(ValueError),
        ):
            run_queue(self.queue, self.journal, "", client=client, handle_result=self.review)
        self.assertEqual(self.requests, [])


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        folder = Path(self.folder.name)
        self.journal = folder / "journal.json"
        self.candidates = folder / "independent-candidates.json"
        self.now = 0
        self.queries = []
        self.anchors = {"chehalis|WA": {"longitude": -122.9, "latitude": 46.6}}
        self.independent = {
            "opis_id": 101,
            "latitude": 46.6001,
            "longitude": -122.9001,
            "source": "independent_public_sources_automated_crosscheck",
            "source_url": "https://www.openstreetmap.org/node/9",
            "external_id": "osm:node:9",
            "matched_name": "Independently cached station name",
            "matched_address": "Independently cached station street",
            "original_names": row()["names"],
            "original_address": row()["address"],
            "original_city": "Chehalis",
            "original_state": "WA",
            "evidence": ["Independent cached premises record"],
        }
        self.contexts = {
            101: {"row": row(), "candidate": self.independent},
            102: {"row": row(opis_id=102, address="1370 Rush Road"), "candidate": None},
            103: {"row": row(opis_id=103), "candidate": None},
        }

    def sleep(self, seconds):
        self.now += seconds

    def recover(self, handler, **options):
        from scripts.coordinates.tomtom_recovery import crosscheck_independent

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return recover_independent(
                self.contexts,
                self.anchors,
                BOUNDARIES,
                "private-test-secret",
                client=client,
                crosscheck=options.pop("crosscheck", crosscheck_independent),
                journal_path=self.journal,
                candidates_path=self.candidates,
                clock=lambda: self.now,
                sleep=self.sleep,
                **options,
            )

    def success(self, request):
        self.queries.append(json.loads(request.content)["query"])
        return httpx.Response(200, json={"results": [result()]})

    def test_real_crosscheck_persists_only_independent_candidate_and_resume_skips_accepted(self):
        outcome = self.recover(self.success)
        self.assertEqual(outcome["independent_candidates_added"], 1)
        self.assertEqual(json.loads(self.candidates.read_text()), [self.independent])
        for path in (self.candidates, self.journal):
            text = path.read_text()
            self.assertNotIn("place-123", text)
            self.assertNotIn("360 555", text)
            self.assertNotIn("private-test-secret", text)
        self.assertEqual(self.recover(self.success)["requests_this_run"], 0)
        self.assertEqual(len(self.queries), 3)

    def test_address_followup_skips_accepted_and_exit_only_rows(self):
        outcome = self.recover(self.success, address_followups=True)
        self.assertEqual(outcome["requests_this_run"], 4)
        self.assertEqual(self.queries, ["Chevron", "Chevron", "Chevron", "1370 Rush Road"])
        self.assertEqual(self.now, 3)

    def test_revalidation_contexts_outside_pending_ids_are_never_searched(self):
        outcome = self.recover(self.success, pending_ids={102})
        self.assertEqual(outcome["requests_this_run"], 1)
        self.assertEqual(outcome["independent_candidates_total"], 0)
        self.assertEqual(self.queries, ["Chevron"])
        journal = json.loads(self.journal.read_text())
        self.assertEqual([attempt["opis_id"] for attempt in journal["attempts"]], [102])

    def test_followup_shares_lifetime_request_budget_with_name_pass(self):
        outcome = self.recover(self.success, address_followups=True, max_requests=3)
        self.assertEqual(outcome["requests_this_run"], 3)
        self.assertEqual(outcome["requests_total"], 3)
        self.assertEqual(outcome["stop"], "request_budget")

    def test_followup_cannot_reset_deadline_or_start_at_deadline(self):
        outcome = self.recover(self.success, address_followups=True, max_seconds=3)
        self.assertEqual(outcome["requests_this_run"], 3)
        self.assertEqual(outcome["stop"], "time_budget")
        self.assertEqual(len(self.queries), 3)

    def test_provider_fields_returned_by_untrusted_callback_are_never_persisted(self):
        outcome = self.recover(
            self.success,
            crosscheck=lambda context, payload, boundaries: {
                **self.independent,
                "provider": payload,
            },
        )
        self.assertEqual(outcome["independent_candidates_added"], 0)
        self.assertFalse(self.candidates.exists())

    def test_independent_point_with_matching_provider_still_must_be_in_original_state(self):
        self.contexts[101]["candidate"] = {**self.independent, "longitude": -115.9999}
        outcome = self.recover(
            self.success, crosscheck=lambda context, payload, boundaries: context["candidate"]
        )
        self.assertEqual(outcome["independent_candidates_added"], 0)
        self.assertFalse(self.candidates.exists())

    def test_stale_existing_candidate_is_not_reused_as_current_source_evidence(self):
        self.candidates.write_text(json.dumps([{**self.independent, "latitude": 46.7}]))
        outcome = self.recover(self.success)
        self.assertEqual(outcome["independent_candidates_added"], 1)
        self.assertEqual(json.loads(self.candidates.read_text()), [self.independent])
        self.assertEqual(len(self.queries), 3)

    def test_fully_journaled_resume_removes_stale_output_without_new_requests(self):
        self.recover(self.success)
        self.candidates.write_text(json.dumps([{**self.independent, "latitude": 46.7}]))

        outcome = self.recover(self.success)

        self.assertEqual(outcome["requests_this_run"], 0)
        self.assertEqual(outcome["independent_candidates_total"], 0)
        self.assertEqual(len(self.queries), 3)
        self.assertEqual(json.loads(self.candidates.read_text()), [])

    def test_halted_queue_removes_disqualified_output_and_retains_valid_candidate(self):
        self.candidates.write_text(
            json.dumps([self.independent, {**self.independent, "opis_id": 102}])
        )
        self.journal.write_text(json.dumps({"version": 1, "halted": "http_429", "attempts": []}))

        outcome = self.recover(self.success)

        self.assertEqual(outcome["stop"], "http_429")
        self.assertEqual(outcome["requests_this_run"], 0)
        self.assertEqual(outcome["independent_candidates_total"], 1)
        self.assertEqual(self.queries, [])
        self.assertEqual(json.loads(self.candidates.read_text()), [self.independent])


if __name__ == "__main__":
    unittest.main()
