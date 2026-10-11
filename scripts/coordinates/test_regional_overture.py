"""Bounded-download and source-geometry regression tests without HTTP calls."""

import importlib
import io
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

overture = importlib.import_module("scripts.coordinates.regional-overture")


class Response(io.BytesIO):
    def __init__(self, body, status=206, content_range="bytes 2-5/10"):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Range": content_range}


class RangeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.cache_patch = patch.object(overture, "CACHE", Path(self.temporary.name))
        self.cache_patch.start()
        self.disk_patch = patch.object(
            overture.shutil, "disk_usage", return_value=SimpleNamespace(free=1024 * 1024 * 1024)
        )
        self.disk_usage = self.disk_patch.start()
        self.addCleanup(self.disk_patch.stop)
        overture.RangeReader.downloaded = 0
        overture.RangeReader.byte_limit = 64 * 1024 * 1024
        overture.RangeReader.last_request = 0
        self.reader = overture.RangeReader("places/part-00001-test.parquet", 10)

    def tearDown(self):
        self.reader.close()
        self.cache_patch.stop()
        self.temporary.cleanup()

    def test_valid_range_is_cached_and_reused_without_another_request(self):
        self.reader.seek(2)
        with patch.object(
            overture.urllib.request, "urlopen", return_value=Response(b"cdef")
        ) as get:
            self.assertEqual(self.reader.read(4), b"cdef")
            self.reader.seek(2)
            self.assertEqual(self.reader.read(4), b"cdef")
        self.assertEqual(get.call_count, 1)
        self.assertEqual(overture.RangeReader.downloaded, 4)

    def test_full_object_response_is_rejected_without_reading_it(self):
        self.reader.seek(2)
        with (
            patch.object(
                overture.urllib.request, "urlopen", return_value=Response(b"abcdefghij", status=200)
            ),
            self.assertRaisesRegex(RuntimeError, "did not honor"),
        ):
            self.reader.read(4)

    def test_wrong_range_header_is_rejected(self):
        self.reader.seek(2)
        with (
            patch.object(
                overture.urllib.request,
                "urlopen",
                return_value=Response(b"cdef", content_range="bytes 0-3/10"),
            ),
            self.assertRaisesRegex(RuntimeError, "different HTTP range"),
        ):
            self.reader.read(4)

    def test_byte_limit_blocks_before_the_request(self):
        overture.RangeReader.byte_limit = 3
        with patch.object(overture.urllib.request, "urlopen") as get:
            with self.assertRaisesRegex(RuntimeError, "byte limit"):
                self.reader.read(4)
            get.assert_not_called()

    def test_download_cannot_reduce_free_disk_below_the_reserve(self):
        self.disk_usage.return_value.free = 512 * 1024 * 1024 + 3
        with patch.object(overture.urllib.request, "urlopen") as get:
            with self.assertRaisesRegex(RuntimeError, "512 MB"):
                self.reader.read(4)
            get.assert_not_called()
        self.assertEqual(overture.RangeReader.downloaded, 0)
        self.assertEqual(list(overture.CACHE.iterdir()), [])

    def test_truncated_cached_payload_is_not_silently_accepted(self):
        (overture.CACHE / "00001-2-4.bin").write_bytes(b"abc")
        self.reader.seek(2)
        with self.assertRaisesRegex(RuntimeError, "wrong length"):
            self.reader.read(4)

    def test_preloaded_range_serves_column_subranges_without_requests(self):
        self.reader.windows = [(0, b"abcdefghij")]
        self.reader.seek(3)
        with patch.object(overture.urllib.request, "urlopen") as get:
            self.assertEqual(self.reader.read(3), b"def")
            get.assert_not_called()

    def test_seek_cannot_escape_the_object(self):
        for position in (-1, 11):
            with self.assertRaises(ValueError):
                self.reader.seek(position)
        self.assertEqual(self.reader.seek(-2, 2), 8)


class SourceGeometryTests(unittest.TestCase):
    def test_point_geometry_keeps_full_precision_in_both_byte_orders(self):
        for prefix, flag in [("<", 1), (">", 0)]:
            value = bytes([flag]) + struct.pack(prefix + "Idd", 1, -88.4495942035222, 44.2727538)
            self.assertEqual(overture.wkb_point(value), (-88.4495942035222, 44.2727538))

    def test_nonpoint_or_nonfinite_geometry_is_rejected(self):
        for kind, longitude in [(2, -88), (1, float("nan"))]:
            value = b"\x01" + struct.pack("<Idd", kind, longitude, 44)
            with self.assertRaises(ValueError):
                overture.wkb_point(value)

    def test_spatial_scope_prunes_mexico_without_certifying_station_identity(self):
        def group(left, bottom, right, top):
            return {
                "columns": [
                    {"path": "bbox.xmin", "min": str(left)},
                    {"path": "bbox.xmax", "max": str(right)},
                    {"path": "bbox.ymin", "min": str(bottom)},
                    {"path": "bbox.ymax", "max": str(top)},
                ]
            }

        self.assertFalse(overture.usa_group(group(-103.5, 20.5, -103.3, 20.8)))
        self.assertTrue(overture.usa_group(group(-74.1, 40.6, -73.9, 40.8)))


if __name__ == "__main__":
    unittest.main()
