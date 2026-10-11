import copy
import json
import pickle
import time
import zlib
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from django.core.cache import cache
from django.core.cache.backends.filebased import FileBasedCache
from django.test import override_settings

from trips.errors import PlanningError
from trips.geometry import GEOD
from trips.provider import MAX_SNAPPING_METERS, CallCounts, HeiGitClient, coordinate_pair

from .helpers import CachedTestCase, geocode_payload, route_payload


class ProviderTests(CachedTestCase):
    def setUp(self):
        super().setUp()
        self.requests, self.responses = [], []
        self.http = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.counts = CallCounts()
        self.provider = HeiGitClient(self.counts, self.http)
        self.points = [[-98, 35], [-96, 35]]

    def tearDown(self):
        self.http.close()
        super().tearDown()

    def handle(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def queue(self, payload=None, status=200, content=None):
        self.responses.append(
            httpx.Response(status, json=payload)
            if content is None
            else httpx.Response(status, content=content)
        )

    def assert_error(self, code, operation, status=None):
        with self.assertRaises(PlanningError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        if status is not None:
            self.assertEqual(caught.exception.status, status)

    def test_current_directions_url_body_and_secret_header(self):
        self.queue(route_payload(self.points, [200]))
        value = self.provider.directions(self.points)
        (request,) = self.requests
        self.assertEqual(
            str(request.url),
            "https://api.heigit.org/openrouteservice/v2/directions/driving-car/geojson",
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["Authorization"], "test-key")
        self.assertEqual(
            json.loads(request.content),
            {
                "coordinates": self.points,
                "radiuses": [350, 350],
                "instructions": True,
                "geometry_simplify": False,
            },
        )
        self.assertEqual(value.distance_miles, Decimal(200))
        self.assertEqual(self.counts.routing, 1)
        self.assertNotIn("test-key", str(request.url))

    def test_current_geocoding_url_and_us_filter(self):
        self.queue(geocode_payload())
        value = self.provider.geocode("Oklahoma City, OK")
        (request,) = self.requests
        self.assertEqual(request.url.path, "/pelias/v1/search")
        self.assertEqual(request.url.params["boundary.country"], "USA")
        self.assertEqual(request.url.params["text"], "Oklahoma City, OK")
        self.assertEqual(request.url.params["size"], "3")
        self.assertEqual(value.coordinates, [-97, 35])
        self.assertEqual(self.counts.geocoding, 1)

    def test_live_geocoding_fixture_preserves_coordinates_and_label(self):
        fixture = Path(__file__).parent / "fixtures" / "heigit-geocode-address.json"
        self.queue(json.loads(fixture.read_text(encoding="utf-8")))
        endpoint = self.provider.geocode("200 North Walker Avenue, Oklahoma City, OK")
        self.assertEqual(endpoint.coordinates, [-97.520271, 35.468927])
        self.assertEqual(endpoint.label, "200 North Walker Avenue, Oklahoma City, OK, USA")
        self.assertEqual(self.counts.geocoding, 1)

    def test_live_route_fixture_retains_road_legs_and_geometry_waypoints(self):
        fixture = Path(__file__).parent / "fixtures" / "heigit-route-with-stop.json"
        self.queue(json.loads(fixture.read_text(encoding="utf-8")))
        route = self.provider.directions(
            [
                [-97.9578, 34.5023],
                [-97.982105974638, 34.158360001914],
                [-97.5861, 33.2343],
            ]
        )
        self.assertEqual(route.leg_meters, (Decimal("40226.9"), Decimal("115377.5")))
        self.assertEqual(sum(route.leg_meters), Decimal("155604.4"))
        self.assertEqual(route.waypoint_indices, (0, 237, 812))
        self.assertEqual(len(route.coordinates), 813)
        progress = route.vertex_miles()
        self.assertAlmostEqual(progress[237], float(route.leg_miles[0]))
        self.assertAlmostEqual(progress[812], float(route.distance_miles))
        self.assertEqual(self.counts.routing, 1)

    def test_requests_leg_distances_for_a_realistic_multi_waypoint_response(self):
        points = [[-98, 35], [-97, 35], [-96, 35]]

        def upstream(request):
            payload = route_payload(points, [100, 200])
            # ORS omits segments when navigation instructions are disabled.
            if not json.loads(request.content).get("instructions", True):
                del payload["features"][0]["properties"]["segments"]
            return httpx.Response(200, json=payload)

        with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
            route = HeiGitClient(CallCounts(), http).directions(points)
        self.assertEqual(route.leg_miles, (Decimal(100), Decimal(200)))
        self.assertEqual(route.distance_miles, Decimal(300))

    def test_geocoding_cache_normalizes_case_and_whitespace(self):
        self.queue(geocode_payload())
        first = self.provider.geocode("Oklahoma   City, OK")
        second = self.provider.geocode(" oklahoma city, ok ")
        self.assertEqual(first, second)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.counts.geocoding_cache_hits, 1)

    def test_route_cache_survives_a_new_request_client(self):
        self.queue(route_payload(self.points, [200]))
        expected = self.provider.directions(self.points)
        other = HeiGitClient(CallCounts(), self.http)
        self.assertEqual(other.directions(self.points), expected)
        self.assertEqual(other.counts.routing, 0)
        self.assertEqual(other.counts.routing_cache_hits, 1)

    def test_nearby_road_snapping_is_accepted(self):
        snapped = [[lon, lat + 0.002] for lon, lat in self.points]
        self.queue(route_payload(snapped, [200]))
        value = self.provider.directions(self.points)
        self.assertEqual(value.coordinates, tuple(map(tuple, snapped)))

    def test_distant_start_stop_and_finish_waypoints_are_rejected(self):
        points = [[-98, 35], [-97, 35], [-96, 35]]
        for index in range(3):
            with self.subTest(waypoint=index):
                self.counts.routing = 0
                snapped = copy.deepcopy(points)
                snapped[index][1] += 0.01
                self.queue(route_payload(snapped, [100, 100]))
                self.assert_error(
                    "invalid_provider_response", lambda: self.provider.directions(points), 502
                )
        self.assertEqual(len(self.requests), 3)

    def test_snapping_limit_allows_coordinate_rounding_only(self):
        for offset, accepted in ((MAX_SNAPPING_METERS + 0.5, True), (352, False)):
            with self.subTest(offset=offset):
                self.counts.routing = 0
                lon, lat, _ = GEOD.fwd(*self.points[0], 0, offset)
                self.queue(route_payload([[lon, lat], self.points[1]], [200]))
                if accepted:
                    self.provider.directions(self.points)
                    cache.clear()
                else:
                    self.assert_error(
                        "invalid_provider_response",
                        lambda: self.provider.directions(self.points),
                        502,
                    )

    def test_corrupt_file_cache_recovers_then_serves_a_cache_hit(self):
        expiry = pickle.dumps(time.time() + 60)
        corruptions = {
            "expiry_pickle": b"invalid expiry",
            "truncated_value": expiry + zlib.compress(b""),
            "value_pickle": expiry + zlib.compress(b"invalid pickle"),
            "compressed_value": expiry + b"invalid compressed value",
        }
        operations = [
            ("directions", self.points, route_payload(self.points, [200]), "routing"),
            ("geocode", "Oklahoma City, OK", geocode_payload(), "geocoding"),
        ]
        for operation_name, argument, payload, counter in operations:
            for corruption_name, content in corruptions.items():
                with (
                    self.subTest(operation=operation_name, corruption=corruption_name),
                    TemporaryDirectory(prefix="fuelroute-cache-test-") as directory,
                ):
                    self.responses.clear()
                    file_cache = FileBasedCache(directory, {})
                    with patch("trips.provider.cache", file_cache):
                        counts = CallCounts()
                        provider = HeiGitClient(counts, self.http)
                        operation = getattr(provider, operation_name)
                        self.queue(payload)
                        expected = operation(argument)
                        (cache_file,) = Path(directory).glob("*.djcache")
                        cache_file.write_bytes(content)

                        def upstream(request, cache_file=cache_file):
                            self.assertFalse(cache_file.exists())
                            return self.handle(request)

                        self.queue(payload)
                        with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
                            recovering = HeiGitClient(counts, http)
                            result = getattr(recovering, operation_name)(argument)
                        self.assertEqual(result, expected)
                        self.assertEqual(getattr(counts, counter), 2)
                        self.assertEqual(getattr(counts, f"{counter}_cache_hits"), 0)
                        self.assertEqual(operation(argument), expected)
                        self.assertEqual(getattr(counts, counter), 2)
                        self.assertEqual(getattr(counts, f"{counter}_cache_hits"), 1)

    def test_close_coordinates_do_not_collide_in_cache(self):
        changed = [[-98.0000001, 35], [-96, 35]]
        self.queue(route_payload(self.points, [200]))
        self.queue(route_payload(changed, [201]))
        self.assertEqual(self.provider.directions(self.points).distance_miles, 200)
        self.assertEqual(self.provider.directions(changed).distance_miles, 201)
        self.assertEqual(self.counts.routing, 2)

    @override_settings(ORS_API_KEY="")
    def test_missing_key_has_zero_network_calls(self):
        self.assert_error(
            "provider_not_configured", lambda: self.provider.directions(self.points), 503
        )
        self.assertEqual(self.counts.routing, 0)
        self.assertEqual(self.requests, [])

    def test_warm_cache_can_be_read_without_key(self):
        self.queue(route_payload(self.points, [200]))
        self.provider.directions(self.points)
        with override_settings(ORS_API_KEY=""):
            self.assertEqual(self.provider.directions(self.points).distance_miles, 200)
        self.assertEqual(self.counts.routing, 1)

    def test_routing_budget_rejects_fourth_uncached_request(self):
        for index in range(3):
            points = [[-98, 35], [-96 + index / 100, 35]]
            self.queue(route_payload(points, [200]))
            self.provider.directions(points)
        self.assert_error(
            "routing_budget_exhausted",
            lambda: self.provider.directions([[-98, 35], [-95, 35]]),
            502,
        )
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.counts.routing, 3)
        self.provider.directions(self.points)
        self.assertEqual(self.counts.routing_cache_hits, 1)

    def test_geocoding_budget_rejects_third_uncached_endpoint(self):
        for text in ["A", "B"]:
            self.queue(geocode_payload())
            self.provider.geocode(text)
        self.assert_error("geocoding_budget_exhausted", lambda: self.provider.geocode("C"))
        self.assertEqual(self.counts.geocoding, 2)

    def test_waypoint_limit_rejects_before_network(self):
        self.assert_error("too_many_stops", lambda: self.provider.directions([[-97, 35]] * 51))
        self.assertEqual(self.requests, [])

    def test_fifty_waypoints_are_supported(self):
        points = [[-98 + i / 100, 35] for i in range(50)]
        self.queue(route_payload(points, [1] * 49))
        self.assertEqual(self.provider.directions(points).distance_miles, 49)

    def test_timeout_is_counted_once_and_not_retried(self):
        self.responses.append(httpx.ReadTimeout("test timeout"))
        self.assert_error("provider_timeout", lambda: self.provider.directions(self.points), 504)
        self.assertEqual(self.counts.routing, 1)
        self.assertEqual(len(self.requests), 1)

    def test_network_failure_is_counted_once_and_not_retried(self):
        self.responses.append(httpx.ConnectError("test connection failure"))
        self.assert_error("provider_unavailable", lambda: self.provider.geocode("A"), 503)
        self.assertEqual(self.counts.geocoding, 1)
        self.assertEqual(len(self.requests), 1)

    def test_upstream_errors_have_controlled_statuses_and_no_secret_echo(self):
        for status, code in [
            (429, "provider_rate_limited"),
            (401, "provider_access_denied"),
            (403, "provider_access_denied"),
            (500, "provider_unavailable"),
            (503, "provider_unavailable"),
        ]:
            with self.subTest(status=status):
                self.counts.routing = 0
                self.queue({"error": "private upstream detail test-key"}, status)
                with self.assertRaises(PlanningError) as caught:
                    self.provider.directions(self.points)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.status, 503)
                self.assertNotIn("test-key", caught.exception.message)

    def test_provider_no_route_error(self):
        for code in (2009, 2010):
            self.queue({"error": {"code": code}}, 404)
            self.assert_error("no_route", lambda: self.provider.directions(self.points), 422)

    def test_unroutable_point_error_explains_how_to_correct_the_location(self):
        self.queue({"error": {"code": 2010}}, 404)
        with self.assertRaises(PlanningError) as caught:
            self.provider.directions(self.points)
        self.assertEqual(caught.exception.code, "no_route")
        self.assertEqual(caught.exception.status, 422)
        self.assertIn("street address or road-accessible coordinates", caught.exception.message)
        self.assertEqual(self.counts.routing, 1)

    def test_generic_rejected_request(self):
        self.queue({"error": {"code": 2003}}, 400)
        self.assert_error(
            "provider_rejected_request", lambda: self.provider.directions(self.points), 422
        )

    def test_non_json_payload(self):
        self.queue(content=b"not json")
        self.assert_error(
            "invalid_provider_response", lambda: self.provider.directions(self.points), 502
        )

    def test_json_array_payload(self):
        self.queue([])
        self.assert_error(
            "invalid_provider_response", lambda: self.provider.directions(self.points), 502
        )

    def test_no_geocoding_results(self):
        self.queue({"features": []})
        self.assert_error("location_not_found", lambda: self.provider.geocode("A"), 422)

    def test_geocoder_cannot_bypass_us_boundary(self):
        for lon, lat, country in [(-79.38, 43.65, "USA"), (-97, 35, "CAN")]:
            self.counts.geocoding = 0
            self.queue(geocode_payload(lon, lat, country=country))
            self.assert_error("location_outside_usa", lambda: self.provider.geocode("A"), 422)

    def test_low_confidence_is_rejected(self):
        self.queue(geocode_payload(confidence=0.6))
        self.assert_error("ambiguous_location", lambda: self.provider.geocode("Springfield"), 422)

    def test_similarly_confident_distant_matches_are_rejected(self):
        payload = geocode_payload()
        payload["features"] += geocode_payload(-74, 40.7, 0.99)["features"]
        self.queue(payload)
        self.assert_error("ambiguous_location", lambda: self.provider.geocode("Main Street"), 422)

    def test_nearby_alternative_matches_are_allowed(self):
        payload = geocode_payload()
        payload["features"] += geocode_payload(-97.001, 35, 0.99)["features"]
        self.queue(payload)
        self.assertEqual(self.provider.geocode("Oklahoma").longitude, -97)

    def test_malformed_geocoding_response(self):
        for payload in [
            {},
            {"features": [{}]},
            geocode_payload(label=None),
            geocode_payload(longitude=True),
        ]:
            with self.subTest(payload=payload):
                self.counts.geocoding = 0
                self.queue(payload)
                self.assert_error(
                    "invalid_provider_response", lambda: self.provider.geocode("A"), 502
                )

    def test_malformed_route_metrics_geometry_and_indices(self):
        changes = [
            ("geometry", "type", "Point"),
            ("geometry", "coordinates", [[-98, 35]]),
            ("geometry", "coordinates", [[-98, 35], [-98, 35]]),
            ("properties", "way_points", [0, 8]),
            ("properties", "way_points", [0, True]),
            ("properties", "way_points", [1, 0]),
            ("properties", "segments", []),
            ("properties", "segments", [{"distance": -1}]),
            ("properties", "segments", [{"distance": "NaN"}]),
            ("properties", "summary", {"distance": 100, "duration": 123}),
            ("properties", "summary", {"distance": 321868.8, "duration": "Infinity"}),
        ]
        for parent, key, value in changes:
            with self.subTest(parent=parent, key=key, value=value):
                self.counts.routing = 0
                payload = copy.deepcopy(route_payload(self.points, [200]))
                payload["features"][0][parent][key] = value
                self.queue(payload)
                self.assert_error(
                    "invalid_provider_response", lambda: self.provider.directions(self.points), 502
                )

    def test_empty_route_is_no_route(self):
        self.queue({"features": []})
        self.assert_error("no_route", lambda: self.provider.directions(self.points))

    def test_falsy_nonlist_route_features_are_invalid_provider_responses(self):
        for value in (None, {}, "", False):
            with self.subTest(features=value):
                self.counts.routing = 0
                self.queue({"features": value})
                self.assert_error(
                    "invalid_provider_response", lambda: self.provider.directions(self.points), 502
                )

    def test_leg_rounding_accumulates_without_rejecting_valid_summary(self):
        points = [[-98 + i / 100, 35] for i in range(50)]
        payload = route_payload(points, [1] * 49)
        properties = payload["features"][0]["properties"]
        properties["segments"] = [{"distance": 1609.3}] * 49
        properties["summary"]["distance"] = 78857.9
        self.queue(payload)
        self.assertEqual(len(self.provider.directions(points).leg_miles), 49)

    def test_geocoding_confidence_and_geometry_schema_are_validated(self):
        for confidence in [True, -0.1, 2]:
            self.counts.geocoding = 0
            self.queue(geocode_payload(confidence=confidence))
            self.assert_error("invalid_provider_response", lambda: self.provider.geocode("A"), 502)
        payload = geocode_payload()
        payload["features"][0]["geometry"]["type"] = "LineString"
        self.counts.geocoding = 0
        self.queue(payload)
        self.assert_error("invalid_provider_response", lambda: self.provider.geocode("A"), 502)

    def test_coordinate_order_and_invalid_values(self):
        self.assertEqual(coordinate_pair([-97, 35]), (-97, 35))
        for value in [
            [35, -97],
            [True, 35],
            ["-97", 35],
            [0, "NaN"],
            [0, float("inf")],
            [181, 0],
            [0],
            None,
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                coordinate_pair(value)

    def test_extreme_geocoder_numbers_have_controlled_errors(self):
        for case, payload in enumerate(
            [geocode_payload(longitude=10**1000), geocode_payload(confidence=10**1000)]
        ):
            with self.subTest(case=case):
                self.counts.geocoding = 0
                self.queue(payload)
                self.assert_error(
                    "invalid_provider_response",
                    lambda case=case: self.provider.geocode(f"Extreme {case}"),
                    502,
                )

    def test_invalid_alternative_confidence_is_rejected(self):
        for case, confidence in enumerate([True, -0.1, 2, "NaN", "Infinity"]):
            with self.subTest(confidence=confidence):
                self.counts.geocoding = 0
                payload = geocode_payload()
                payload["features"] += geocode_payload(-97.001, 35, confidence)["features"]
                self.queue(payload)
                self.assert_error(
                    "invalid_provider_response",
                    lambda case=case: self.provider.geocode(f"Alternative {case}"),
                    502,
                )

    def test_deeply_nested_provider_json_is_rejected(self):
        self.queue(content=b"[" * 2000 + b"0" + b"]" * 2000)
        self.assert_error(
            "invalid_provider_response", lambda: self.provider.directions(self.points), 502
        )
