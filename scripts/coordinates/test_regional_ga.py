"""Test source boundaries, false identity joins and one-attempt acquisition failures."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from scripts.coordinates import regional_ga as ga


def retailer(**changes):
    return {
        "id": "31",
        "name": "Friendly Express #31",
        "address": "1700 N Second St",
        "city": "Folkston",
        "state": "GA",
        "lat": 30.844,
        "lng": -82.008,
        **changes,
    }


def station(name="FRIENDLY EXPRESS #31", city="Folkston", state="GA"):
    return {"names": [name], "city": city, "state": state}


class RetailerIdentityTests(unittest.TestCase):
    def test_numbered_sunstop_feed_stays_unverified_and_excludes_wrong_branch(self):
        row = {
            "store": "SunStop #889",
            "permalink": "https://sunstopstores.com/stores/sunstop-889/",
            "address": "16375 US19 N",
            "city": "Thomasville",
            "state": "GA",
            "lat": "30.938194",
            "lng": "-84.011406",
        }
        records = ga.sunstop_points([row, {**row, "state": "FL"}, {**row, "store": "SunStop #819"}])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["external_id"], "sunstop:889")
        self.assertFalse(records[0]["coordinate_verified"])
        self.assertNotIn("latitude", records[0])
        self.assertEqual(ga.sunstop_points([{**row, "lat": "NaN"}]), [])
        with self.assertRaises(TypeError):
            ga.sunstop_points({"stores": []})

    def test_map_destination_ignores_viewport_and_rejects_multiple_places(self):
        base = "https://www.google.com/maps/place/Gas+Pro/@32,-81,17z/data="
        url = base + "!4m6!3m5!1sid!8m2!3d33.1014192!4d-81.9970492"
        self.assertEqual(
            ga.map_destination_point(url),
            {"latitude": 33.1014192, "longitude": -81.9970492},
        )
        for invalid in (
            base,
            url.replace("www.google.com", "example.test"),
            url.replace("https://", "http://"),
            base + "!8m2!3d91!4d-81",
            url + "!8m2!3d32!4d-80",
        ):
            with self.subTest(url=invalid):
                self.assertIsNone(ga.map_destination_point(invalid))

    def test_primary_feed_markup_empty_tail_and_native_geocodes(self):
        value = {
            "locations": [
                {
                    "title": "Store #31",
                    "url_title": "store-31",
                    "address": "<p>6008 Second St. N</p>",
                    "city": "Folkston",
                    "state": "GA",
                    "coordinates": ["30.858193", "-82.010041"],
                },
                {},
            ]
        }
        rows = ga.friendly_express_points(json.dumps(value))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["external_id"], "friendly-express:31")
        self.assertEqual(rows[0]["street"], "6008 Second St. N")
        self.assertFalse(rows[0]["coordinate_verified"])
        self.assertNotIn("latitude", rows[0])
        value["locations"][0]["coordinates"][0] = "NaN"
        self.assertEqual(ga.friendly_express_points(json.dumps(value)), [])

    def test_public_retailer_native_point_is_only_a_search_lead(self):
        record = ga.retailer_records({"retailers": [retailer()]}, "https://example.test")[0]
        self.assertFalse(record["coordinate_verified"])
        self.assertFalse(record["identity_verified"])
        self.assertNotIn("latitude", record)
        self.assertEqual(record["street"], "1700 N Second St")
        self.assertEqual(record["unverified_search_point"]["latitude"], 30.844)

    def test_wrong_state_missing_address_and_nonfinite_points(self):
        rows = [
            retailer(state="AL"),
            retailer(address=""),
            retailer(lat=float("nan")),
            retailer(lat=True),
        ]
        records = ga.retailer_records({"retailers": rows}, "https://example.test")
        self.assertEqual(len(records), 2)
        self.assertTrue(all("unverified_search_point" not in row for row in records))
        with self.assertRaises(TypeError):
            ga.retailer_records({"locations": []}, "https://example.test")

    def test_numbered_identity_wrong_numbers_city_and_state(self):
        record = ga.retailer_records({"retailers": [retailer()]}, "https://example.test")[0]
        self.assertEqual(
            ga.identity_lead(station(), record), "same_distinctive_name_city_state_address_lead"
        )
        for row in (station("Friendly Express #32"), station(city="Savannah"), station(state="FL")):
            with self.subTest(row=row):
                self.assertIsNone(ga.identity_lead(row, record))

    def test_generic_brands_never_match_from_city_alone(self):
        for name in ("BP", "Shell", "Chevron", "Marathon"):
            with self.subTest(name=name):
                record = ga.retailer_records(
                    {"retailers": [retailer(name=name)]}, "https://example.test"
                )[0]
                self.assertIsNone(ga.identity_lead(station(name), record))


class RetailerAcquisitionTests(unittest.TestCase):
    def run_query(self, response, directory):
        client = httpx.Client(transport=httpx.MockTransport(lambda request: response(request)))
        with (
            client,
            patch.object(ga.shutil, "disk_usage", return_value=SimpleNamespace(free=10**9)),
        ):
            return ga.download_query("friendly express", directory, client)

    def test_capped_success_is_cached_without_second_request(self):
        calls = []

        def response(request):
            calls.append(request)
            return httpx.Response(200, json={"retailers": [retailer()], "page": 1, "totalPages": 1})

        with tempfile.TemporaryDirectory(dir=ga.SOURCES) as temp:
            directory = Path(temp)
            self.assertEqual(len(self.run_query(response, directory)), 1)
            self.assertEqual(len(self.run_query(response, directory)), 1)
            self.assertEqual(len(calls), 1)

    def test_http_failures_are_preserved_and_never_retried(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status), tempfile.TemporaryDirectory(dir=ga.SOURCES) as temp:
                directory = Path(temp)
                with self.assertRaises(httpx.HTTPStatusError):
                    self.run_query(lambda request, status=status: httpx.Response(status), directory)
                report = json.loads(
                    (
                        directory / "regional-ga-lottery-friendly-express-acquisition.json"
                    ).read_text()
                )
                self.assertEqual(report["http_status"], status)
                self.assertEqual(report["status"], "failed")
                with self.assertRaises(FileExistsError):
                    self.run_query(lambda request: self.fail("Repeated request"), directory)

    def test_oversized_response_and_low_disk_do_not_save_source(self):
        with tempfile.TemporaryDirectory(dir=ga.SOURCES) as temp:
            directory = Path(temp)
            with (
                patch.object(ga, "MAX_BYTES", 20),
                self.assertRaisesRegex(ValueError, "download limit"),
            ):
                self.run_query(lambda request: httpx.Response(200, content=b"x" * 21), directory)
            self.assertFalse((directory / "regional-ga-lottery-friendly-express.json").exists())
        with tempfile.TemporaryDirectory(dir=ga.SOURCES) as temp:
            directory = Path(temp)
            with (
                patch.object(ga.shutil, "disk_usage", return_value=SimpleNamespace(free=1)),
                httpx.Client(
                    transport=httpx.MockTransport(lambda request: self.fail("Low-disk request"))
                ) as client,
                self.assertRaisesRegex(ValueError, "Insufficient free space"),
            ):
                ga.download_query("friendly express", directory, client)


if __name__ == "__main__":
    unittest.main()
