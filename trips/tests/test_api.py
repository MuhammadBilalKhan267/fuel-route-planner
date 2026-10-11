import json
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

import httpx
from django.core.handlers.asgi import ASGIRequest
from django.test import override_settings

from trips.importing import refresh_revision
from trips.provider import HeiGitClient
from trips.views import plan_route

from .helpers import CachedTestCase, create_station, geocode_payload, route_payload


class ApiTests(CachedTestCase):
    def setUp(self):
        super().setUp()
        self.requests, self.responses = [], []
        self.http = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.provider_patch = patch(
            "trips.views.HeiGitClient", side_effect=lambda counts: HeiGitClient(counts, self.http)
        )
        self.provider_patch.start()
        self.start, self.middle, self.finish = [-100, 35], [-99, 35], [-98, 35]
        self.body = {
            "start": {"longitude": -100, "latitude": 35},
            "finish": {"longitude": -98, "latitude": 35},
        }

    def tearDown(self):
        self.provider_patch.stop()
        self.http.close()
        super().tearDown()

    def handle(self, request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected extra network call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def queue_route(self, points, lengths, indices=None):
        self.responses.append(httpx.Response(200, json=route_payload(points, lengths, indices)))

    def post(self, **changes):
        return self.client.post(
            "/api/routes/", {**self.body, **changes}, content_type="application/json"
        )

    def assert_ok(self, response):
        self.assertEqual(response.status_code, 200, response.content.decode())
        result = response.json()
        fuel = result["fuel"]
        self.assertEqual(
            Decimal(fuel["initial_fuel_gallons"])
            + Decimal(fuel["gallons_purchased"])
            - Decimal(fuel["gallons_consumed"]),
            Decimal(fuel["ending_fuel_gallons"]),
        )
        self.assertEqual(
            Decimal(fuel["total_cost_usd"]),
            Decimal(fuel["initial_fuel_cost_usd"])
            + sum((Decimal(stop["cost_usd"]) for stop in fuel["stops"]), Decimal(0)),
        )
        self.assertLessEqual(result["metadata"]["routing_calls"], 3)
        for stop in fuel["stops"]:
            self.assertGreater(Decimal(stop["gallons_purchased"]), 0)
            self.assertGreaterEqual(Decimal(stop["arrival_fuel_gallons"]), 0)
            self.assertLessEqual(Decimal(stop["departure_fuel_gallons"]), 50)
        return result

    def setup_long_trip(self):
        create_station(longitude=-99, latitude=35)
        self.queue_route([self.start, self.middle, self.finish], [800], [0, 2])

    def test_short_trip_one_route_zero_geocoding(self):
        self.queue_route([self.start, self.finish], [200])
        result = self.assert_ok(self.post())
        self.assertEqual(result["metadata"]["routing_calls"], 1)
        self.assertEqual(result["metadata"]["geocoding_calls"], 0)
        self.assertEqual(result["fuel"]["stops"], [])
        self.assertEqual(result["fuel"]["total_cost_usd"], "0.00")
        self.assertEqual(result["route"]["geometry"]["type"], "LineString")
        self.assertEqual(result["route"]["geometry"]["coordinates"][0], self.start)

    def test_two_text_endpoints_use_two_geocoding_calls(self):
        self.responses.extend(
            [
                httpx.Response(200, json=geocode_payload(*self.start)),
                httpx.Response(200, json=geocode_payload(*self.finish)),
            ]
        )
        self.queue_route([self.start, self.finish], [200])
        result = self.assert_ok(self.post(start="Start, OK", finish={"address": "Finish, OK"}))
        self.assertEqual(result["metadata"]["geocoding_calls"], 2)
        self.assertEqual(result["metadata"]["routing_calls"], 1)

    def test_mixed_endpoint_types_use_one_geocoding_call(self):
        self.responses.append(httpx.Response(200, json=geocode_payload(*self.start)))
        self.queue_route([self.start, self.finish], [200])
        result = self.assert_ok(self.post(start="Start, OK"))
        self.assertEqual(result["metadata"]["geocoding_calls"], 1)

    def test_default_verification_routes_through_stops_and_recalculates_cost(self):
        self.setup_long_trip()
        self.queue_route([self.start, self.middle, self.finish], [410, 410])
        result = self.assert_ok(self.post())
        self.assertTrue(result["planning"]["verify_stops_requested"])
        self.assertTrue(result["planning"]["road_distances_verified"])
        self.assertEqual(result["route"]["distance_miles"], "820.000000")
        self.assertEqual(result["route"]["via_station_ids"], [1])
        self.assertEqual(result["fuel"]["total_cost_usd"], "96.00")
        self.assertEqual(result["fuel"]["stops"][0]["route_mile"], "410.000000")
        self.assertEqual(result["fuel"]["stops"][0]["arrival_fuel_gallons"], "9.0000000")
        self.assertEqual(
            json.loads(self.requests[1].content)["coordinates"],
            [self.start, self.middle, self.finish],
        )
        self.assertEqual(result["metadata"]["routing_calls"], 2)
        self.assertFalse(result["planning"]["globally_optimal_road_route"])

    def test_verification_off_returns_estimate_and_one_route_call(self):
        self.setup_long_trip()
        result = self.assert_ok(self.post(verify_stops=False))
        self.assertFalse(result["planning"]["road_distances_verified"])
        self.assertEqual(result["planning"]["distance_model"], "base_route_station_projection")
        self.assertEqual(result["route"]["distance_miles"], "800.000000")
        self.assertEqual(result["fuel"]["total_cost_usd"], "90.00")
        self.assertEqual(result["metadata"]["routing_calls"], 1)
        self.assertEqual(result["route"]["via_station_ids"], [])

    @override_settings(DEFAULT_VERIFY_STOPS=False)
    def test_environment_can_set_verification_default_off(self):
        self.setup_long_trip()
        result = self.assert_ok(self.post())
        self.assertFalse(result["planning"]["verify_stops_requested"])
        self.assertEqual(len(self.requests), 1)

    def test_initial_fuel_and_its_price_are_configurable(self):
        create_station(longitude=-99, latitude=35)
        self.queue_route([self.start, self.middle, self.finish], [100], [0, 2])
        self.queue_route([self.start, self.middle, self.finish], [50, 50])
        result = self.assert_ok(
            self.post(initial_fuel_gallons="5", initial_fuel_price_usd_per_gallon="2.5")
        )
        self.assertEqual(result["fuel"]["initial_fuel_cost_usd"], "12.50")
        self.assertEqual(result["fuel"]["purchase_cost_usd"], "15.00")
        self.assertEqual(result["fuel"]["total_cost_usd"], "27.50")

    @override_settings(DEFAULT_INITIAL_FUEL_GALLONS="10", DEFAULT_INITIAL_FUEL_PRICE_USD="2")
    def test_environment_can_change_initial_defaults(self):
        self.queue_route([self.start, self.finish], [50])
        result = self.assert_ok(self.post())
        self.assertEqual(result["fuel"]["initial_fuel_gallons"], "10.0000000")
        self.assertEqual(result["fuel"]["ending_fuel_gallons"], "5.0000000")
        self.assertEqual(result["fuel"]["total_cost_usd"], "20.00")

    def test_repeated_text_trip_can_use_zero_provider_calls(self):
        self.responses.extend(
            [
                httpx.Response(200, json=geocode_payload(*self.start)),
                httpx.Response(200, json=geocode_payload(*self.finish)),
            ]
        )
        self.queue_route([self.start, self.finish], [200])
        self.assert_ok(self.post(start="Start, OK", finish="Finish, OK"))
        result = self.assert_ok(self.post(start="Start, OK", finish="Finish, OK"))
        self.assertEqual(result["metadata"]["routing_calls"], 0)
        self.assertEqual(result["metadata"]["geocoding_calls"], 0)
        self.assertEqual(result["metadata"]["routing_cache_hits"], 1)
        self.assertEqual(result["metadata"]["geocoding_cache_hits"], 2)

    def test_prices_refresh_even_when_geometry_is_cached(self):
        self.setup_long_trip()
        self.queue_route([self.start, self.middle, self.finish], [400, 400])
        self.assertEqual(self.assert_ok(self.post())["fuel"]["total_cost_usd"], "90.00")
        from trips.models import FuelStation

        FuelStation.objects.filter(opis_id=1).update(price=Decimal("1.5"))
        refresh_revision()
        result = self.assert_ok(self.post())
        self.assertEqual(result["fuel"]["total_cost_usd"], "45.00")
        self.assertEqual(result["metadata"]["routing_calls"], 0)
        self.assertEqual(result["metadata"]["routing_cache_hits"], 2)

    def test_actual_road_detour_can_trigger_one_correction(self):
        self.setup_long_trip()
        auxiliary = [-99.5, 35]
        create_station(2, longitude=auxiliary[0], latitude=35, price="5")
        self.queue_route([self.start, auxiliary, self.middle, self.finish], [510, 310], [0, 2, 3])
        self.queue_route([self.start, auxiliary, self.middle, self.finish], [260, 260, 310])
        result = self.assert_ok(self.post())
        self.assertTrue(result["planning"]["correction_used"])
        self.assertEqual(result["metadata"]["routing_calls"], 3)
        self.assertEqual(result["route"]["via_station_ids"], [2, 1])
        self.assertEqual(result["route"]["distance_miles"], "830.000000")
        self.assertEqual(result["fuel"]["total_cost_usd"], "103.00")

    def test_failed_correction_returns_error_instead_of_unverified_success(self):
        self.setup_long_trip()
        auxiliary = [-99.5, 35]
        create_station(2, longitude=auxiliary[0], latitude=35, price="5")
        self.queue_route([self.start, auxiliary, self.middle, self.finish], [510, 310], [0, 2, 3])
        self.queue_route([self.start, auxiliary, self.middle, self.finish], [510, 10, 310])
        response = self.post()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "verified_plan_unavailable")
        self.assertEqual(response.json()["metadata"]["routing_calls"], 3)

    def test_no_local_correction_candidate_returns_controlled_error(self):
        self.setup_long_trip()
        self.queue_route([self.start, self.middle, self.finish], [510, 310])
        response = self.post()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "verified_plan_unavailable")
        self.assertEqual(len(self.requests), 2)

    def test_verification_provider_failure_is_not_silently_downgraded(self):
        self.setup_long_trip()
        self.responses.append(httpx.Response(503, json={}))
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["metadata"]["routing_calls"], 2)
        self.assertEqual(response.json()["error"]["code"], "provider_unavailable")

    def test_unnecessary_purchase_waypoint_is_removed_with_third_call(self):
        other = [-98.5, 35]
        create_station(1, longitude=-99, latitude=35, price="4")
        create_station(2, longitude=-98.5, latitude=35, price="2")
        self.queue_route([self.start, self.middle, other, self.finish], [1000], [0, 3])
        self.queue_route([self.start, self.middle, other, self.finish], [400, 90, 460])
        self.queue_route([self.start, other, self.finish], [490, 460])
        result = self.assert_ok(self.post())
        self.assertEqual(result["route"]["via_station_ids"], [2])
        self.assertEqual([s["station_id"] for s in result["fuel"]["stops"]], [2])
        self.assertEqual(result["metadata"]["routing_calls"], 3)
        self.assertEqual(result["fuel"]["total_cost_usd"], "90.00")

    def test_no_fourth_call_when_last_route_needs_no_purchases(self):
        other = [-98.5, 35]
        create_station(1, longitude=-99, latitude=35, price="4")
        create_station(2, longitude=-98.5, latitude=35, price="2")
        self.queue_route([self.start, self.middle, other, self.finish], [1000], [0, 3])
        self.queue_route([self.start, self.middle, other, self.finish], [400, 90, 460])
        self.queue_route([self.start, other, self.finish], [200, 200])
        result = self.assert_ok(self.post())
        self.assertEqual(result["fuel"]["stops"], [])
        self.assertEqual(result["route"]["via_station_ids"], [2])
        self.assertEqual(len(self.requests), 3)

    def test_long_route_without_station_coordinates_is_infeasible(self):
        create_station(latitude=None, longitude=None)
        self.queue_route([self.start, self.finish], [800])
        response = self.post()
        self.assertEqual(response.status_code, 422)
        error = response.json()["error"]
        self.assertEqual(error["code"], "fuel_plan_unavailable")
        self.assertEqual(error["details"]["mapped_stations"], 0)
        self.assertEqual(error["details"]["us_stations"], 1)

    def test_csv_station_geocoding_never_occurs_during_trip(self):
        create_station(latitude=None, longitude=None)
        self.queue_route([self.start, self.finish], [200])
        result = self.assert_ok(self.post())
        self.assertEqual(result["metadata"]["geocoding_calls"], 0)
        self.assertEqual(len(self.requests), 1)

    def test_identical_coordinate_endpoints_do_not_call_provider(self):
        response = self.post(finish=self.body["start"])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "identical_locations")
        self.assertEqual(self.requests, [])

    def test_distinct_texts_resolving_to_same_place_are_rejected(self):
        self.responses.extend(
            [
                httpx.Response(200, json=geocode_payload()),
                httpx.Response(200, json=geocode_payload()),
            ]
        )
        response = self.post(start="A", finish="B")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["metadata"]["routing_calls"], 0)

    def test_invalid_options_are_rejected_before_network(self):
        invalid = {
            "initial_fuel_gallons": [
                0,
                -1,
                51,
                True,
                None,
                "NaN",
                "Infinity",
                "0.00000001",
                "1e999",
                "1e-1000000",
            ],
            "initial_fuel_price_usd_per_gallon": [-1, True, None, "NaN", "0.000000001", 1001],
            "verify_stops": ["true", 1, None, []],
            "station_corridor_miles": [0, -1, 21, True, "0.0000001"],
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    response = self.post(**{field: value})
                    self.assertEqual(response.status_code, 400, response.content.decode())
        self.assertEqual(self.requests, [])

    def test_invalid_location_shapes_and_numeric_types(self):
        values = [
            "",
            " " * 10,
            "A" * 301,
            None,
            True,
            [],
            [-100, 35],
            {},
            {"address": 3},
            {"latitude": 35},
            {"latitude": 35, "longitude": "-100"},
            {"latitude": True, "longitude": -100},
            {"latitude": 91, "longitude": -100},
            {"latitude": 35, "longitude": -181},
            {"latitude": 35, "longitude": -100, "address": "A"},
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(self.post(start=value).status_code, 400)
        self.assertEqual(self.requests, [])

    def test_us_validation_rejects_non_us_coordinates_before_network(self):
        response = self.post(start={"latitude": 43.65, "longitude": -79.38})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "location_outside_usa")
        self.assertEqual(self.requests, [])

    def test_unknown_fields_missing_fields_and_non_object_body(self):
        for payload in [{}, {"start": "A"}, {**self.body, "unexpected": 1}, [], None, "text"]:
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/api/routes/", json.dumps(payload), content_type="application/json"
                )
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.requests, [])

    def test_invalid_json_nonfinite_and_duplicate_fields(self):
        for payload in [
            "{",
            "",
            '{"start":NaN}',
            '{"start":Infinity}',
            '{"start":"A","start":"B","finish":"C"}',
            '{"start":{"latitude":35,"latitude":36,"longitude":-100},"finish":"A"}',
        ]:
            with self.subTest(payload=payload):
                response = self.client.generic(
                    "POST",
                    "/api/routes/",
                    payload,
                    content_type="application/json",
                    CONTENT_TYPE="application/json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_json")

    def test_invalid_utf8(self):
        self.assertEqual(
            self.client.post("/api/routes/", b"\xff", content_type="application/json").status_code,
            400,
        )

    def test_unpaired_surrogates_in_endpoint_text_fail_before_provider_calls(self):
        for text in ["Oklahoma City, OK \ud800", "Dallas, TX \udc00", "\ud800\ud800"]:
            for endpoint in ["start", "finish"]:
                with self.subTest(text=repr(text), endpoint=endpoint):
                    response = self.post(**{endpoint: text})
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["error"]["code"], "invalid_request")
                    self.assertEqual(response.json()["metadata"]["geocoding_calls"], 0)
        self.assertEqual(self.requests, [])

    def test_valid_supplementary_unicode_in_endpoint_text_is_preserved(self):
        self.responses.append(httpx.Response(200, json=geocode_payload(*self.start)))
        self.queue_route([self.start, self.finish], [200])
        text = "Oklahoma City, OK \U0001f697"
        self.assert_ok(self.post(start=text))
        self.assertEqual(self.requests[0].url.params["text"], text)

    def test_other_json_encodings_are_rejected_before_provider_calls(self):
        self.queue_route([self.start, self.finish], [200])
        for encoding in ["utf-16", "utf-32"]:
            with self.subTest(encoding=encoding):
                response = self.client.post(
                    "/api/routes/",
                    json.dumps(self.body).encode(encoding),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_json")
        self.assertEqual(self.requests, [])

    def test_body_limit_uses_actual_payload_even_when_length_is_underreported(self):
        body = json.dumps(self.body).encode() + b" " * 16384
        request = ASGIRequest(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/routes/",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json"), (b"content-length", b"0")],
            },
            BytesIO(body),
        )
        self.queue_route([self.start, self.finish], [200])
        response = plan_route(request)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.requests, [])

    def test_deeply_nested_json_is_bad_request_instead_of_server_error(self):
        payload = "[" * 1800 + "0" + "]" * 1800
        response = self.client.post("/api/routes/", payload, content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_json")

    def test_numeric_precision_ignores_only_trailing_zeroes(self):
        self.queue_route([self.start, self.finish], [200])
        result = self.assert_ok(
            self.post(
                initial_fuel_gallons="50.000000000",
                initial_fuel_price_usd_per_gallon="0e9999999999",
            )
        )
        self.assertEqual(result["fuel"]["initial_fuel_gallons"], "50.0000000")
        self.assertEqual(result["fuel"]["initial_fuel_price_usd_per_gallon"], "0.00000000")

    def test_map_page_has_defaults_and_no_provider_secret(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "trips/map.html")
        self.assertEqual(response.context["defaults"]["initial_fuel_gallons"], "50")
        self.assertTrue(response.context["defaults"]["verify_stops"])
        self.assertNotContains(response, "test-key")
        self.assertContains(response, "map-tiles")

    def test_unexpected_error_does_not_leak_implementation_details(self):
        with (
            patch("trips.views.plan_trip", side_effect=RuntimeError("simulated private detail")),
            self.assertLogs("trips.views", level="ERROR"),
        ):
            response = self.post()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "internal_error")
        self.assertNotIn("simulated private detail", response.content.decode())

    def test_oversized_body(self):
        response = self.client.post("/api/routes/", "x" * 16385, content_type="application/json")
        self.assertEqual(response.status_code, 413)

    def test_media_type_and_method_errors(self):
        response = self.client.get("/api/routes/")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")
        self.assertEqual(self.client.post("/api/routes/", self.body).status_code, 415)

    @override_settings(ORS_API_KEY="")
    def test_missing_provider_key_returns_controlled_service_error(self):
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "provider_not_configured")
        self.assertEqual(response.json()["metadata"]["routing_calls"], 0)

    def test_health_reports_configuration_and_coordinate_coverage(self):
        create_station()
        create_station(2, latitude=None, longitude=None)
        response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["stations"]["mapped_stations"], 1)
        self.assertEqual(response.json()["stations"]["us_stations"], 2)
        self.assertEqual(self.requests, [])
