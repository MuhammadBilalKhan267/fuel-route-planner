"""Check printed directory identities and bounded, one-attempt source acquisition."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from scripts.coordinates import exit_directories as directories


def station(name="Rhodes 101 Stop #790", city="Scott City", state="MO"):
    return {"names": [name], "City": city, "State": state}


class PrintedDirectoryTests(unittest.TestCase):
    def test_city_route_phone_columns_and_pdf_pages_remain_linked(self):
        text = (
            "Directory header\nScott City, MO    Rhodes 101 Stop #790\n"
            "                  I-55 Exit 91          (573) 335-8200\n"
            "\fFountain, CO    Tomahawk Travel Center\n"
            "                  I-25 Exit 128          (719) 382-5473\n"
        )
        records, rejected = directories.nastc_records(text)
        self.assertEqual(rejected, [])
        self.assertEqual([item["page"] for item in records], [1, 2])
        self.assertEqual(records[0]["route_description"], "I-55 Exit 91")
        self.assertEqual(records[0]["phone"], "573-335-8200")
        self.assertFalse(any(item["coordinate_verified"] for item in records))
        self.assertTrue(all("latitude" not in item for item in records))

    def test_malformed_printed_phones_preserve_identity_without_repair(self):
        for raw_phone in (
            "(928) 5285-2431",
            "(831 385-7572",
            "712-3 346-5025",
            "(315) 4399-4040",
            "(580) )307-9005",
        ):
            with self.subTest(raw_phone=raw_phone):
                records, rejected = directories.nastc_records(
                    f"Example, OK    Named Truck Stop\n    US-62          {raw_phone}\n"
                )
                self.assertEqual(rejected, [])
                self.assertEqual(records[0]["route_description"], "US-62")
                self.assertEqual(records[0]["raw_phone"], raw_phone)
                self.assertIsNone(records[0]["phone"])
                self.assertEqual(records[0]["phone_status"], "malformed_printed_phone")

    def test_publisher_test_and_non_us_rows_are_explicitly_excluded(self):
        records, rejected = directories.nastc_records(
            "Nowhere, OK    Test\n\fToronto, ON    Named Truck Stop\n"
            "    Hwy 401          (416) 123-4567\n"
        )
        self.assertEqual(records, [])
        self.assertEqual(len(rejected), 2)
        self.assertTrue(
            all(item["reason"] == "non_us_or_publisher_test_record" for item in rejected)
        )

    def test_missing_route_is_not_borrowed_from_next_station_or_pdf_page(self):
        records, rejected = directories.nastc_records(
            "First, MO    First Truck Stop\nSecond, MO    Second Truck Stop\n"
            "    I-55 Exit 91          (573) 335-8200\n"
            "Third, MO    Third Truck Stop\f    I-55 Exit 92          (573) 335-8201\n"
        )
        self.assertEqual([item["name"] for item in records], ["Second Truck Stop"])
        self.assertEqual(
            [item["name"] for item in rejected], ["First Truck Stop", "Third Truck Stop"]
        )

    def test_exact_distinctive_identity_and_numbered_brand(self):
        self.assertEqual(
            directories.nastc_identity(
                station(), {"name": "Rhodes 101 Stop #790", "city": "Scott City", "state": "MO"}
            ),
            "same_distinctive_name_city_state",
        )
        self.assertEqual(
            directories.nastc_identity(
                station("Speedway #1221", "Calvert City", "KY"),
                {"name": "Speedway #1221", "city": "Calvert City", "state": "KY"},
            ),
            "same_published_brand_store_number_city_state",
        )

    def test_generic_brands_wrong_numbers_city_and_state_are_not_identity(self):
        for original, actual in (
            (station("Shell"), {"name": "Shell"}),
            (station("Speedway #1221"), {"name": "Speedway #1222"}),
            (station(), {"name": "Rhodes 101 Stop #790", "city": "Cape Girardeau"}),
            (station(), {"name": "Rhodes 101 Stop #790", "state": "IL"}),
        ):
            with self.subTest(original=original, actual=actual):
                record = {"city": "Scott City", "state": "MO", **actual}
                self.assertIsNone(directories.nastc_identity(original, record))


class DirectoryAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(dir=directories.ROOT)
        self.sources = Path(self.workspace.name)
        self.patch_sources = patch.object(directories, "SOURCES", self.sources)
        self.patch_sources.start()
        self.addCleanup(self.patch_sources.stop)
        self.addCleanup(self.workspace.cleanup)
        self.patch_disk_space = patch.object(
            directories.shutil, "disk_usage", return_value=SimpleNamespace(free=2 * 1024**3)
        )
        self.patch_disk_space.start()
        self.addCleanup(self.patch_disk_space.stop)

    def test_insufficient_disk_prevents_request_and_acquisition_ledger(self):
        with (
            patch.object(directories.shutil, "disk_usage", return_value=SimpleNamespace(free=0)),
            patch.object(directories.httpx, "Client") as client,
            self.assertRaisesRegex(RuntimeError, "Insufficient free disk space"),
        ):
            directories.download("nastc", directories.DIRECTORIES["nastc"])
        client.assert_not_called()
        self.assertFalse((self.sources / "regional-exit-nastc-directory.pdf").exists())
        self.assertFalse((self.sources / "regional-exit-nastc-acquisition.json").exists())

    def test_cache_has_no_http_effect_and_rejects_non_pdf(self):
        target = self.sources / "regional-exit-nastc-directory.pdf"
        target.write_bytes(b"%PDF-1.7\ncache")
        with patch.object(directories.httpx, "Client") as client:
            self.assertEqual(
                directories.download("nastc", directories.DIRECTORIES["nastc"]), target
            )
            client.assert_not_called()
        target.write_bytes(b"html")
        with self.assertRaisesRegex(ValueError, "not a PDF"):
            directories.download("nastc", directories.DIRECTORIES["nastc"])

    def test_http_error_is_preserved_and_blocks_later_attempt(self):
        for status in (401, 403, 429, 500):
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory(dir=self.sources) as folder,
                patch.object(directories, "SOURCES", Path(folder)),
            ):
                calls = []
                client = httpx.Client(
                    transport=httpx.MockTransport(
                        lambda request, calls=calls, status=status: (
                            calls.append(request) or httpx.Response(status)
                        )
                    )
                )
                with (
                    patch.object(directories.httpx, "Client", return_value=client),
                    self.assertRaises(httpx.HTTPStatusError),
                ):
                    directories.download("nastc", directories.DIRECTORIES["nastc"])
                ledger = json.loads(
                    (Path(folder) / "regional-exit-nastc-acquisition.json").read_text()
                )
                self.assertEqual(ledger["http_status"], status)
                self.assertEqual(ledger["status"], "failed")
                self.assertEqual(ledger["downloaded_bytes"], 0)
                with self.assertRaisesRegex(RuntimeError, "prior acquisition"):
                    directories.download("nastc", directories.DIRECTORIES["nastc"])
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].method, "GET")

    def test_successful_source_is_hashed_and_declared_byte_cap_prevents_write(self):
        body = b"%PDF-1.7\npublic directory"
        client = httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
        )
        with patch.object(directories.httpx, "Client", return_value=client):
            target = directories.download("nastc", directories.DIRECTORIES["nastc"])
        self.assertEqual(target.read_bytes(), body)
        ledger = json.loads((self.sources / "regional-exit-nastc-acquisition.json").read_text())
        self.assertEqual(ledger["status"], "complete")
        self.assertEqual(ledger["downloaded_bytes"], len(body))
        self.assertEqual(len(ledger["sha256"]), 64)
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"Content-Length": str(directories.MAX_BYTES + 1)}, content=body
                )
            )
        )
        with (
            patch.object(directories.httpx, "Client", return_value=client),
            self.assertRaisesRegex(ValueError, "Declared PDF"),
        ):
            directories.download("coam", directories.DIRECTORIES["coam"])
        self.assertFalse((self.sources / "regional-exit-coam-directory.pdf").exists())


if __name__ == "__main__":
    unittest.main()
