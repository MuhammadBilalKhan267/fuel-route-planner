"""Regression coverage for routable city lookups and precise endpoint safeguards."""

import copy
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
from django.conf import settings
from django.core.cache import cache

from trips.errors import PlanningError
from trips.fuel import miles
from trips.provider import CallCounts, Endpoint, HeiGitClient, cache_key

from .helpers import CachedTestCase

FIXTURES = Path(__file__).parent / "fixtures"
SOURCES = "openstreetmap,openaddresses,geonames"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class CityGeocodingTests(CachedTestCase):
    def setUp(self):
        super().setUp()
        self.requests, self.responses = [], []
        self.http = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.counts = CallCounts()
        self.provider = HeiGitClient(self.counts, self.http)
        self.provider_patch = patch(
            "trips.views.HeiGitClient", side_effect=lambda counts: HeiGitClient(counts, self.http)
        )
        self.provider_patch.start()

    def tearDown(self):
        self.provider_patch.stop()
        self.http.close()
        super().tearDown()

    def handle(self, request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected extra provider call")
        return httpx.Response(200, json=self.responses.pop(0))

    def queue_city_trip(self):
        self.responses.extend(
            [
                fixture("heigit-geocode-oklahoma-city.json"),
                fixture("heigit-geocode-dallas.json"),
                fixture("heigit-route-oklahoma-dallas.json"),
            ]
        )

    def post_trip(self, **changes):
        return self.client.post(
            "/api/routes/",
            {"start": "Oklahoma City, OK", "finish": "Dallas, TX", **changes},
            content_type="application/json",
        )

    def assert_geocode_error(self, text, payload, code="ambiguous_location", status=422):
        self.responses.append(payload)
        with self.assertRaises(PlanningError) as caught:
            self.provider.geocode(text)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.status, status)

    def test_city_trip_replays_live_provider_responses_at_api_boundary(self):
        self.queue_city_trip()
        response = self.post_trip()
        self.assertEqual(response.status_code, 200, response.content.decode())
        result = response.json()
        self.assertEqual(result["start"]["coordinates"], [-97.51643, 35.46756])
        self.assertEqual(result["finish"]["coordinates"], [-96.80667, 32.78306])
        self.assertEqual(result["route"]["geometry"]["type"], "LineString")
        self.assertEqual(
            result["route"]["geometry"],
            fixture("heigit-route-oklahoma-dallas.json")["features"][0]["geometry"],
        )
        expected_miles = miles(Decimal("329457.4") / Decimal("1609.344"))
        self.assertEqual(Decimal(result["route"]["distance_miles"]), expected_miles)
        fuel = result["fuel"]
        self.assertEqual(fuel["initial_fuel_gallons"], "50.0000000")
        self.assertEqual(fuel["total_cost_usd"], "0.00")
        self.assertEqual(fuel["stops"], [])
        self.assertEqual(Decimal(fuel["gallons_consumed"]), expected_miles / 10)
        self.assertEqual(
            Decimal(fuel["initial_fuel_gallons"])
            + Decimal(fuel["gallons_purchased"])
            - Decimal(fuel["gallons_consumed"]),
            Decimal(fuel["ending_fuel_gallons"]),
        )
        self.assertEqual(result["metadata"]["geocoding_calls"], 2)
        self.assertEqual(result["metadata"]["routing_calls"], 1)
        self.assertTrue(result["planning"]["road_distances_verified"])
        self.assertEqual(len(self.requests), 3)
        for request in self.requests[:2]:
            self.assertEqual(request.url.params["sources"], SOURCES)
            self.assertEqual(request.url.params["boundary.country"], "USA")
        body = json.loads(self.requests[-1].content)
        self.assertEqual(
            body["coordinates"], [result["start"]["coordinates"], result["finish"]["coordinates"]]
        )
        self.assertEqual(body["radiuses"], [350, 350])

    def test_normalized_repeat_uses_geocoding_and_route_caches_without_http(self):
        self.queue_city_trip()
        first = self.post_trip()
        self.assertEqual(first.status_code, 200, first.content.decode())
        repeated = self.post_trip(
            start="  OKLAHOMA   CITY,   ok  ", finish={"address": "  DALLAS,   tx  "}
        )
        self.assertEqual(repeated.status_code, 200, repeated.content.decode())
        result = repeated.json()
        self.assertEqual(result["route"], first.json()["route"])
        self.assertEqual(result["fuel"], first.json()["fuel"])
        self.assertEqual(result["metadata"]["geocoding_calls"], 0)
        self.assertEqual(result["metadata"]["routing_calls"], 0)
        self.assertEqual(result["metadata"]["geocoding_cache_hits"], 2)
        self.assertEqual(result["metadata"]["routing_cache_hits"], 1)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.responses, [])

    def test_city_input_accepts_whole_name_with_state_and_country_variants(self):
        queries = (
            "Dallas",
            "Dallas, TX",
            "Dallas TX",
            "Dallas, Texas",
            "Dallas Texas",
            "Dallas, TX, USA",
            "Dallas TX USA",
            "Dallas, Texas, United States",
            "Dallas Texas United States",
        )
        for text in queries:
            with self.subTest(text=text):
                self.responses.append(fixture("heigit-geocode-dallas.json"))
                self.assertEqual(self.provider.geocode(text).coordinates, [-96.80667, 32.78306])
                self.counts.geocoding = 0
        self.assertEqual(self.responses, [])

    def test_localadmin_city_matches_its_complete_name(self):
        payload = fixture("heigit-geocode-oklahoma-city.json")
        payload["features"] = [payload["features"][1]]
        self.responses.append(payload)
        endpoint = self.provider.geocode("City of Oklahoma City, Oklahoma")
        self.assertEqual(endpoint.coordinates, [-97.51379, 35.46765])

    def test_dotted_dc_and_us_abbreviations_keep_state_validation(self):
        payload = fixture("heigit-geocode-dallas.json")
        payload["features"] = [payload["features"][0]]
        payload["features"][0]["geometry"]["coordinates"] = [-77.0369, 38.9072]
        payload["features"][0]["properties"].update(
            name="Washington",
            locality="Washington",
            region="District of Columbia",
            region_a="DC",
            label="Washington, DC, USA",
        )
        for text in ("Washington, D.C.", "Washington, D.C., U.S."):
            with self.subTest(text=text):
                self.responses.append(copy.deepcopy(payload))
                self.assertEqual(self.provider.geocode(text).coordinates, [-77.0369, 38.9072])
                self.counts.geocoding = 0
        self.assert_geocode_error("Washington, M.D., U.S.", payload)

    def test_distant_lake_dallas_result_does_not_make_exact_dallas_ambiguous(self):
        self.responses.append(fixture("heigit-geocode-dallas.json"))
        endpoint = self.provider.geocode("Dallas, TX")
        self.assertEqual(endpoint.coordinates, [-96.80667, 32.78306])

    def test_state_free_same_city_name_in_distant_states_is_ambiguous(self):
        payload = fixture("heigit-geocode-dallas.json")
        payload["features"] = [payload["features"][0], copy.deepcopy(payload["features"][0])]
        for feature, coordinates, state, abbreviation in zip(
            payload["features"],
            [[-89.65015, 39.80172], [-93.29824, 37.21533]],
            ["Illinois", "Missouri"],
            ["IL", "MO"],
            strict=True,
        ):
            feature["geometry"]["coordinates"] = coordinates
            feature["properties"].update(
                name="Springfield",
                locality="Springfield",
                region=state,
                region_a=abbreviation,
                label=f"Springfield, {abbreviation}, USA",
            )
        self.assert_geocode_error("Springfield", payload)

    def test_explicit_state_disambiguates_distant_same_city_name(self):
        payload = fixture("heigit-geocode-dallas.json")
        alternative = copy.deepcopy(payload["features"][0])
        alternative["geometry"]["coordinates"] = [-123.30333, 44.91928]
        alternative["properties"].update(region="Oregon", region_a="OR", label="Dallas, OR, USA")
        payload["features"] = [payload["features"][0], alternative]
        self.responses.append(payload)
        self.assertEqual(self.provider.geocode("Dallas, TX").coordinates, [-96.80667, 32.78306])

    def test_city_fallback_for_requested_street_fails_before_routing(self):
        payload = fixture("heigit-geocode-oklahoma-city.json")
        payload["features"][0]["properties"]["match_type"] = "fallback"
        self.responses.append(payload)
        response = self.post_trip(start="200 North Walker Avenue, Oklahoma City, OK")
        self.assertEqual(response.status_code, 422, response.content.decode())
        self.assertEqual(response.json()["error"]["code"], "ambiguous_location")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].method, "GET")

    def test_street_query_cannot_accept_city_even_with_exact_provider_match_type(self):
        self.assert_geocode_error(
            "200 North Walker Avenue, Oklahoma City, OK",
            fixture("heigit-geocode-oklahoma-city.json"),
        )

    def test_city_fallback_match_type_is_rejected_even_when_name_matches(self):
        payload = fixture("heigit-geocode-dallas.json")
        payload["features"][0]["properties"]["match_type"] = "fallback"
        self.assert_geocode_error("Dallas, TX", payload)

    def test_city_name_state_and_country_must_match_entire_query(self):
        for text in ("Lake Dallas, TX", "Dallas, OK", "Dallas, TX, Canada", "Dallas County, TX"):
            with self.subTest(text=text):
                self.assert_geocode_error(text, fixture("heigit-geocode-dallas.json"))
                self.counts.geocoding = 0

    def test_city_filter_does_not_hide_malformed_alternative_confidence(self):
        payload = fixture("heigit-geocode-dallas.json")
        payload["features"][2]["properties"]["confidence"] = True
        self.assert_geocode_error("Dallas, TX", payload, "invalid_provider_response", 502)

    def test_precise_address_keeps_existing_distant_alternative_guard(self):
        payload = fixture("heigit-geocode-dallas.json")
        payload["features"] = [payload["features"][0], copy.deepcopy(payload["features"][0])]
        payload["features"][0]["properties"].update(layer="address", name="200 Main Street")
        payload["features"][1]["properties"].update(layer="address", name="200 Main Street")
        payload["features"][1]["geometry"]["coordinates"] = [-97.51643, 35.46756]
        self.assert_geocode_error("200 Main Street, TX", payload)

    def test_old_unfiltered_geocoding_cache_cannot_restore_polygon_centroid(self):
        old_key = cache_key(
            "geocode",
            {"base": settings.ORS_API_BASE_URL, "text": "oklahoma city, ok"},
        )
        cache.set(old_key, Endpoint(-97.602513, 35.393761, "Oklahoma City, OK, USA"))
        self.responses.append(fixture("heigit-geocode-oklahoma-city.json"))
        endpoint = self.provider.geocode("Oklahoma City, OK")
        self.assertEqual(endpoint.coordinates, [-97.51643, 35.46756])
        self.assertEqual(self.counts.geocoding, 1)
        self.assertEqual(self.counts.geocoding_cache_hits, 0)
        self.assertEqual(self.requests[0].url.params["sources"], SOURCES)
