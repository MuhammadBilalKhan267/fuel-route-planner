"""Tests for exact exits, conservative identities and one-attempt acquisition limits."""

import csv
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from shapely.geometry import box

from scripts.coordinates import exit_anchors as exits


def row(name="PINE RIDGE TRUCK STOP", address="I-95, EXIT 22A", city="Example"):
    return {"names": [name], "Address": address, "City": city, "State": "VA"}


def poi(name="Pine Ridge Truck Stop", **tags):
    return {
        "type": "way",
        "id": 99,
        "latitude": 38.001,
        "longitude": -77.001,
        "tags": {"name": name, "amenity": "fuel", **tags},
    }


class ExitParserTests(unittest.TestCase):
    def test_exact_suffix_normalizes_case_spacing_and_leading_zeros(self):
        self.assertEqual(exits.exit_ref(" 0022-a "), "22A")
        self.assertEqual(exits.exit_ref("22 B"), "22B")
        self.assertNotEqual(exits.exit_ref("22"), exits.exit_ref("22A"))

    def test_ranges_and_descriptions_are_not_single_exit_refs(self):
        for value in ("22A/B", "22-24", "22 AB", "EXIT 22", "", "22.5"):
            with self.subTest(value=value):
                self.assertIsNone(exits.exit_ref(value))

    def test_multiple_routes_remain_explicit(self):
        self.assertEqual(
            exits.csv_exit_request("I-80/I-90, EXIT 13 & US-20"),
            {"routes": ["I-80", "I-90"], "exit_ref": "13"},
        )
        self.assertEqual(exits.route_refs("I-095; US 95; Interstate 295"), {"I-95", "I-295"})

    def test_lettered_interstates_and_business_routes_do_not_collapse(self):
        self.assertEqual(
            exits.route_refs("I-35E; I-35W; I-69C; I-H-1"), {"I-35E", "I-35W", "I-69C", "I-H1"}
        )
        self.assertEqual(exits.route_refs("I-35 BUS; I-80 Business; I-90 ALT"), set())

    def test_old_new_numbers_and_multiple_exit_tokens_are_ambiguous(self):
        for address in (
            "I-70, EXIT 12/161",
            "I-95, EXITS 22A/22B",
            "I-76 EXIT 146 & I-99 EXIT 1",
            "I-95 EXIT 22A-B",
            "I-80 Exit 234 WB/234-B EB",
        ):
            with self.subTest(address=address):
                self.assertIsNone(exits.csv_exit_request(address))

    def test_cross_road_text_does_not_become_suffix(self):
        self.assertEqual(exits.csv_exit_request("I-12, EXIT 10 AT MILE 10")["exit_ref"], "10")
        self.assertEqual(exits.csv_exit_request("I-75, EXIT 144-B")["exit_ref"], "144B")
        self.assertIsNone(exits.csv_exit_request("US-12 EXIT 22"))


class AnchorTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "elements": [
                {
                    "type": "node",
                    "id": 1,
                    "lat": 38,
                    "lon": -77,
                    "tags": {"highway": "motorway_junction", "ref": "22A;22B"},
                },
                {
                    "type": "way",
                    "id": 2,
                    "nodes": [0, 1, 3],
                    "tags": {"highway": "motorway", "ref": "I 95; I 295"},
                },
                {
                    "type": "way",
                    "id": 3,
                    "nodes": [1, 4],
                    "tags": {"highway": "motorway_link", "ref": "I 64"},
                },
            ]
        }
        self.boundaries = {"VA": box(-78, 37, -76, 39), "MD": box(-76, 37, -74, 39)}

    def test_only_actual_parent_motorways_and_exact_states_bind(self):
        anchors = exits.extract_anchors(self.snapshot, self.boundaries)
        self.assertEqual(len(anchors), 4)
        self.assertEqual({point["route"] for point in anchors}, {"I-95", "I-295"})
        self.assertEqual({point["exit_ref"] for point in anchors}, {"22A", "22B"})
        self.assertEqual({point["state"] for point in anchors}, {"VA"})
        self.assertTrue(all(not point["coordinate_verified"] for point in anchors))
        self.assertTrue(all(point["purpose"] == "exit_search_anchor_only" for point in anchors))

    def test_unreferenced_parent_and_nonfinite_junction_never_create_anchor(self):
        self.snapshot["elements"][1]["tags"].pop("ref")
        self.assertEqual(exits.extract_anchors(self.snapshot, self.boundaries), [])
        self.snapshot["elements"][1]["tags"]["ref"] = "I 95"
        self.snapshot["elements"][0]["lat"] = float("nan")
        self.assertEqual(exits.extract_anchors(self.snapshot, self.boundaries), [])

    def test_matching_does_not_relax_exit_suffix_or_state(self):
        anchors = exits.extract_anchors(self.snapshot, self.boundaries)
        proposals, missed = exits.build_proposals(
            {1: row(address="I-95 EXIT 22")}, anchors, {"VA": [poi()]}
        )
        self.assertEqual(proposals, [])
        self.assertEqual(len(missed), 1)
        source = row()
        source["State"] = "MD"
        proposals, missed = exits.build_proposals({1: source}, anchors, {"VA": [poi()]})
        self.assertEqual(proposals, [])
        self.assertEqual(len(missed), 1)

    def test_named_fuel_is_proposal_only_and_wrong_city_cannot_bind(self):
        anchors = exits.extract_anchors(self.snapshot, self.boundaries)
        proposals, _ = exits.build_proposals(
            {1: row()}, anchors, {"VA": [poi(**{"addr:city": "Other"})]}
        )
        self.assertEqual(len(proposals), 1)
        self.assertFalse(proposals[0]["coordinate_verified"])
        self.assertIsNone(proposals[0]["fuel_candidates"][0]["identity_rule"])

    def test_convenience_shop_and_distant_fuel_are_excluded(self):
        anchors = exits.extract_anchors(self.snapshot, self.boundaries)
        shop = poi()
        shop["tags"]["amenity"] = "shop"
        distant = poi()
        distant["latitude"] = 38.1
        proposals, _ = exits.build_proposals({1: row()}, anchors, {"VA": [shop, distant]})
        self.assertEqual(proposals[0]["fuel_candidates"], [])


class IdentityTests(unittest.TestCase):
    def test_generic_brand_without_explicit_store_number_is_not_identity(self):
        self.assertIsNone(exits.strict_identity(row("SHELL #123"), poi("Shell")))
        self.assertIsNone(exits.strict_identity(row("SHELL"), poi("Shell")))

    def test_brand_and_actual_store_number_match(self):
        self.assertEqual(
            exits.strict_identity(row("CASEYS #123"), poi("Casey's", **{"ref:store": "00123"})),
            "same_brand_and_explicit_store_number",
        )
        self.assertIsNone(exits.strict_identity(row("CASEYS #123"), poi("Casey's #124")))

    def test_distinctive_name_is_identity_but_still_needs_premises_review(self):
        self.assertEqual(exits.strict_identity(row(), poi()), "exact_distinctive_station_name")
        self.assertIsNone(exits.strict_identity(row("TRUCK STOP"), poi("Truck Stop")))


class AcquisitionTests(unittest.TestCase):
    def request(self, directory, handler, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return exits.download_snapshot(directory, client=client, **kwargs)

    def test_success_is_cached_and_second_execution_makes_zero_requests(self):
        requests = []
        with tempfile.TemporaryDirectory(dir=exits.ROOT) as folder:
            directory = Path(folder)

            def handler(request):
                requests.append(request)
                return httpx.Response(
                    200, json={"elements": [], "osm3s": {"timestamp_osm_base": "2026-10-11"}}
                )

            self.request(directory, handler)
            self.request(directory, lambda _: self.fail("cached execution called the API"))
            self.assertEqual(len(requests), 1)
            audit = json.loads((directory / "regional-exit-acquisition.json").read_text())
            self.assertEqual(audit["status"], "complete")
            self.assertLess(
                audit["prior_compute_reservation_seconds"] + audit["server_timeout_seconds"], 600
            )

    def test_http_failure_is_recorded_and_never_retried(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status), tempfile.TemporaryDirectory(dir=exits.ROOT) as folder:
                directory = Path(folder)
                with self.assertRaises(httpx.HTTPStatusError):
                    self.request(directory, lambda _, status=status: httpx.Response(status))
                with self.assertRaises(FileExistsError):
                    self.request(directory, lambda _: self.fail("failed acquisition was retried"))
                self.assertFalse((directory / "regional-exit-snapshot.json").exists())
                audit = json.loads((directory / "regional-exit-acquisition.json").read_text())
                self.assertEqual(audit["http_status"], status)

    def test_declared_and_streamed_download_limits_abort_without_cache(self):
        for headers in ({"content-length": "100"}, {"content-length": "0"}):
            with (
                self.subTest(headers=headers),
                tempfile.TemporaryDirectory(dir=exits.ROOT) as folder,
                patch.object(exits, "MAX_DOWNLOAD", 4),
            ):
                directory = Path(folder)
                with self.assertRaisesRegex(ValueError, "download budget"):
                    self.request(
                        directory,
                        lambda _, headers=headers: httpx.Response(
                            200, content=b"12345", headers=headers
                        ),
                    )
                self.assertFalse((directory / "regional-exit-snapshot.json").exists())

    def test_incomplete_overpass_remark_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=exits.ROOT) as folder:
            directory = Path(folder)
            with self.assertRaisesRegex(ValueError, "incomplete"):
                self.request(
                    directory,
                    lambda _: httpx.Response(
                        200, json={"elements": [], "remark": "runtime timeout"}
                    ),
                )
            self.assertFalse((directory / "regional-exit-snapshot.json").exists())
            self.assertEqual(
                json.loads((directory / "regional-exit-failed-response.json").read_text())[
                    "remark"
                ],
                "runtime timeout",
            )

    def test_elapsed_time_budget_aborts(self):
        moments = iter((0, 111, 112))
        with tempfile.TemporaryDirectory(dir=exits.ROOT) as folder:
            directory = Path(folder)
            with self.assertRaises(TimeoutError):
                self.request(
                    directory,
                    lambda _: httpx.Response(200, json={"elements": []}),
                    clock=lambda: next(moments),
                )
            self.assertFalse((directory / "regional-exit-snapshot.json").exists())

    def test_compute_reservation_prevents_request(self):
        with (
            tempfile.TemporaryDirectory(dir=exits.ROOT) as folder,
            patch.object(exits, "PRIOR_COMPUTE_SECONDS", 510),
            self.assertRaisesRegex(ValueError, "ten minutes"),
        ):
            self.request(Path(folder), lambda _: self.fail("exhausted budget called the API"))


class PublishedReleaseTests(unittest.TestCase):
    def setUp(self):
        disk_space = patch.object(
            exits.shutil, "disk_usage", return_value=SimpleNamespace(free=2 * 1024**3)
        )
        disk_space.start()
        self.addCleanup(disk_space.stop)

    def fixture(self, directory):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, value in {
                "csv/corridor_exits.csv": b"exit_id,exit_number\nnode/1,22A\n",
                "csv/places.csv": b"place_id,name\nnode/2,Pine Ridge\n",
                "source_lineage.json": b"{}",
                "manifest.json": b"{}",
            }.items():
                info = tarfile.TarInfo(name)
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
        content = buffer.getvalue()
        metadata = {
            "tag_name": "test",
            "assets": [
                {
                    "name": "release.tar.gz",
                    "size": len(content),
                    "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                    "browser_download_url": "https://github.com/example/releases/download/test/release.tar.gz",
                }
            ],
        }
        (directory / "regional-exit-openinterstate-release.json").write_text(json.dumps(metadata))
        return content, metadata

    def test_checksum_verified_release_extracts_only_named_tables_and_reuses_cache(self):
        with tempfile.TemporaryDirectory(dir=exits.ROOT) as folder:
            directory = Path(folder)
            content, _ = self.fixture(directory)
            with httpx.Client(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content))
            ) as client:
                exits.published_release(directory, client)
            with httpx.Client(
                transport=httpx.MockTransport(
                    lambda _: self.fail("published cache repeated request")
                )
            ) as client:
                exits.published_release(directory, client)
            self.assertTrue((directory / "regional-exit-published-exits.csv").exists())
            self.assertFalse((directory / "csv").exists())

    def test_bad_archive_checksum_is_not_cached(self):
        with tempfile.TemporaryDirectory(dir=exits.ROOT) as folder:
            directory = Path(folder)
            content, _ = self.fixture(directory)
            wrong = content[:-1] + bytes([content[-1] ^ 1])
            with (
                httpx.Client(
                    transport=httpx.MockTransport(lambda _: httpx.Response(200, content=wrong))
                ) as client,
                self.assertRaisesRegex(ValueError, "checksum"),
            ):
                exits.published_release(directory, client)
            self.assertFalse((directory / "regional-exit-openinterstate.tar.gz").exists())

    def test_published_archive_byte_budget_prevents_any_request(self):
        with tempfile.TemporaryDirectory(dir=exits.ROOT) as folder:
            directory = Path(folder)
            _, metadata = self.fixture(directory)
            metadata["assets"][0]["size"] = exits.MAX_DOWNLOAD + 1
            (directory / "regional-exit-openinterstate-release.json").write_text(
                json.dumps(metadata)
            )
            with self.assertRaisesRegex(ValueError, "byte budget"):
                exits.published_release(directory)

    def test_insufficient_disk_prevents_request_and_download_ledger(self):
        with tempfile.TemporaryDirectory(dir=exits.ROOT) as folder:
            directory = Path(folder)
            self.fixture(directory)
            with (
                patch.object(exits.shutil, "disk_usage", return_value=SimpleNamespace(free=0)),
                httpx.Client(
                    transport=httpx.MockTransport(
                        lambda _: self.fail("insufficient disk space called the API")
                    )
                ) as client,
                self.assertRaisesRegex(ValueError, "Insufficient free space"),
            ):
                exits.published_release(directory, client)
            self.assertFalse((directory / "regional-exit-openinterstate.tar.gz").exists())
            self.assertFalse((directory / "regional-exit-published-acquisition.json").exists())

    def test_published_exits_preserve_branch_suffixes_and_search_only_status(self):
        with tempfile.TemporaryDirectory(dir=exits.ROOT) as folder:
            path = Path(folder) / "exits.csv"
            fields = [
                "exit_id",
                "interstate_name",
                "exit_number",
                "lon",
                "lat",
                "corridor_id",
                "direction_code",
            ]
            with path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fields)
                writer.writeheader()
                base = {
                    "exit_id": "node/1",
                    "interstate_name": "I-35E",
                    "exit_number": "22A;22B",
                    "lon": -77,
                    "lat": 38,
                    "corridor_id": "1",
                    "direction_code": "north",
                }
                writer.writerow(base)
                writer.writerow({**base, "exit_id": "node/2", "lat": "nan"})
                writer.writerow({**base, "exit_id": "way/3"})
            anchors = exits.published_anchors(
                path, {"VA": box(-78, 37, -76, 39)}, "https://github.com/example/release"
            )
            self.assertEqual(len(anchors), 2)
            self.assertEqual({anchor["route"] for anchor in anchors}, {"I-35E"})
            self.assertEqual({anchor["exit_ref"] for anchor in anchors}, {"22A", "22B"})
            self.assertTrue(all(not anchor["coordinate_verified"] for anchor in anchors))


if __name__ == "__main__":
    unittest.main()
