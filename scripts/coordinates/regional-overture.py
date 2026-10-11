"""Inspect public Overture Parquet ranges with explicit request and byte limits."""

import argparse
import io
import json
import math
import shutil
import struct
import time
import urllib.request
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

SOURCES = Path(__file__).resolve().parents[2] / "data/coordinate-sources"
CACHE = SOURCES / "regional-overture-ranges"


class RangeReader(io.RawIOBase):
    """Seekable, cached HTTP ranges; never fall back to a whole-object download."""

    last_request = 0.0
    downloaded = 0
    byte_limit = 64 * 1024 * 1024

    def __init__(self, key, size):
        super().__init__()
        self.key, self.size, self.position = key, size, 0
        self.prefix = key.rsplit("/", 1)[1].split("-")[1]
        self.windows = []
        CACHE.mkdir(exist_ok=True)

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        origin = (0, self.position, self.size)[whence]
        target = origin + offset
        if target < 0 or target > self.size:
            raise ValueError("Seek is outside the public object")
        self.position = target
        return target

    def read(self, length=-1):
        length = min(length if length >= 0 else self.size, self.size - self.position)
        if length == 0:
            return b""
        start = self.position
        cache = CACHE / f"{self.prefix}-{start}-{length}.bin"
        containing = next(
            (
                (offset, body)
                for offset, body in self.windows
                if offset <= start and start + length <= offset + len(body)
            ),
            None,
        )
        if containing:
            offset, body = containing
            body = body[start - offset : start - offset + length]
        elif cache.exists():
            body = cache.read_bytes()
        else:
            if RangeReader.downloaded + length > RangeReader.byte_limit:
                raise RuntimeError("Public-range download byte limit reached")
            if shutil.disk_usage(CACHE).free - length < 512 * 1024 * 1024:
                raise RuntimeError("Preserving at least 512 MB of free workspace disk")
            time.sleep(max(0, 1 - (time.monotonic() - RangeReader.last_request)))
            RangeReader.last_request = time.monotonic()
            request = urllib.request.Request(
                "https://overturemaps-us-west-2.s3.amazonaws.com/" + self.key,
                headers={
                    "Range": f"bytes={start}-{start + length - 1}",
                    "User-Agent": "AssessmentStationResearch/1.0",
                },
            )
            with urllib.request.urlopen(request, timeout=45) as response:
                if response.status != 206:
                    raise RuntimeError("Provider did not honor the bounded HTTP range")
                expected = f"bytes {start}-{start + length - 1}/{self.size}"
                if response.headers.get("Content-Range") != expected:
                    raise RuntimeError("Provider returned a different HTTP range")
                body = response.read(length + 1)
            if len(body) != length:
                raise RuntimeError("Truncated or oversized HTTP range")
            cache.write_bytes(body)
            RangeReader.downloaded += len(body)
        if len(body) != length:
            raise RuntimeError("Cached or downloaded HTTP range has the wrong length")
        self.position += len(body)
        return body

    def preload(self, ranges):
        """Coalesce nearby column chunks to avoid many tiny public requests."""
        merged = []
        for start, size in sorted(ranges):
            end = start + size
            if merged and start - merged[-1][1] <= 16384:
                merged[-1][1] = max(end, merged[-1][1])
            else:
                merged.append([start, end])
        previous = self.position
        self.windows = []
        for start, end in merged:
            self.seek(start)
            self.windows.append((start, self.read(end - start)))
        self.position = previous


def objects():
    root = ET.fromstring((SOURCES / "regional-overture-places-index.xml").read_bytes())
    ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
    return [
        (record.find("s:Key", ns).text, int(record.find("s:Size", ns).text))
        for record in root.findall("s:Contents", ns)
    ]


def inspect(limit):
    from pyarrow import parquet

    output = []
    for key, size in objects()[:limit]:
        with RangeReader(key, size) as source:
            file = parquet.ParquetFile(source)
            groups = []
            for index in range(file.metadata.num_row_groups):
                group = file.metadata.row_group(index)
                columns = []
                for number in range(group.num_columns):
                    column = group.column(number)
                    stat = column.statistics
                    columns.append(
                        {
                            "path": column.path_in_schema,
                            "compressed_bytes": column.total_compressed_size,
                            "offset": min(
                                value
                                for value in (
                                    column.dictionary_page_offset,
                                    column.data_page_offset,
                                )
                                if value is not None
                            ),
                            "min": str(stat.min) if stat and stat.has_min_max else None,
                            "max": str(stat.max) if stat and stat.has_min_max else None,
                        }
                    )
                groups.append({"index": index, "rows": group.num_rows, "columns": columns})
            output.append({"key": key, "bytes": size, "groups": groups})
    (SOURCES / "regional-overture-metadata.json").write_text(
        json.dumps(output, indent=2) + "\n", encoding="utf-8"
    )
    print({"objects": len(output), "downloaded_bytes": RangeReader.downloaded})


@lru_cache(maxsize=1)
def usa_query_area():
    from shapely import make_valid
    from shapely.geometry import shape
    from shapely.ops import unary_union

    states = json.loads(
        (SOURCES.parents[1] / "trips/data/us-states.geojson").read_text(encoding="utf-8")
    )
    return unary_union(
        [
            make_valid(shape(feature["geometry"]))
            for feature in states["features"]
            if feature["properties"]["STUSAB"] not in {"AK", "HI", "PR", "VI", "GU", "MP", "AS"}
        ]
    )


def usa_group(group):
    from shapely.geometry import box

    columns = {column["path"]: column for column in group["columns"]}
    left, right = float(columns["bbox.xmin"]["min"]), float(columns["bbox.xmax"]["max"])
    bottom, top = float(columns["bbox.ymin"]["min"]), float(columns["bbox.ymax"]["max"])
    # State geometry scopes downloads only; it does not verify any station identity.
    return usa_query_area().intersects(box(left, bottom, right, top))


def column_ranges(group, fields):
    return [
        (column["offset"], column["compressed_bytes"])
        for column in group["columns"]
        if any(
            column["path"] == field or column["path"].startswith(field + ".") for field in fields
        )
    ]


def wkb_point(value):
    if len(value) != 21 or value[0] not in (0, 1):
        raise ValueError("Expected a two-dimensional WKB point")
    kind, longitude, latitude = struct.unpack(("<" if value[0] else ">") + "Idd", value[1:])
    if kind != 1 or not all(math.isfinite(number) for number in (longitude, latitude)):
        raise ValueError("Invalid source point geometry")
    return longitude, latitude


def extract(group_limit):
    from pyarrow import parquet

    metadata = json.loads((SOURCES / "regional-overture-metadata.json").read_text(encoding="utf-8"))
    categories = {"gas_station", "truck_stop", "convenience_store", "service_station", "rest_stop"}
    total, checked = 0, 0
    fields = ["id", "geometry", "names.primary", "addresses"]
    for item in metadata:
        part = item["key"].rsplit("/", 1)[1].split("-")[1]
        selected = [group for group in item["groups"] if usa_group(group)]
        if not selected:
            continue
        with RangeReader(item["key"], item["bytes"]) as source:
            file = parquet.ParquetFile(source)
            for group in selected:
                if checked >= group_limit:
                    print(
                        {
                            "groups_checked": checked,
                            "records": total,
                            "downloaded_bytes": RangeReader.downloaded,
                        }
                    )
                    return
                destination = SOURCES / f"regional-overture-pois-{part}-{group['index']:04}.json"
                checked += 1
                if destination.exists():
                    total += len(json.loads(destination.read_text(encoding="utf-8")))
                    continue
                source.preload(column_ranges(group, ["basic_category", "operating_status"]))
                initial = file.read_row_group(
                    group["index"],
                    columns=["basic_category", "operating_status"],
                    use_threads=False,
                )
                basic = initial["basic_category"].to_pylist()
                status = initial["operating_status"].to_pylist()
                indices = [index for index, category in enumerate(basic) if category in categories]
                records = []
                if indices:
                    source.preload(column_ranges(group, fields))
                    table = file.read_row_group(group["index"], columns=fields, use_threads=False)
                    for index in indices:
                        row = table.slice(index, 1).to_pylist()[0]
                        addresses = [
                            address
                            for address in (row.get("addresses") or [])
                            if address.get("country") == "US"
                        ]
                        if not addresses:
                            continue
                        longitude, latitude = wkb_point(row["geometry"])
                        records.append(
                            {
                                "external_id": row["id"],
                                "name": row["names"]["primary"],
                                "latitude": latitude,
                                "longitude": longitude,
                                "category": basic[index],
                                "addresses": addresses,
                                "operating_status": status[index],
                                "source_url": "https://overturemaps-us-west-2.s3.amazonaws.com/"
                                + item["key"],
                                "source_record": {
                                    "row_group": group["index"],
                                    "row_in_group": index,
                                },
                                "retrieved_on": "2026-10-11",
                                "coordinate_verified": False,
                            }
                        )
                temporary = destination.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(records) + "\n", encoding="utf-8")
                temporary.replace(destination)
                total += len(records)
                if checked % 20 == 0:
                    print(
                        {
                            "groups_checked": checked,
                            "records": total,
                            "downloaded_bytes": RangeReader.downloaded,
                        },
                        flush=True,
                    )
    print({"groups_checked": checked, "records": total, "downloaded_bytes": RangeReader.downloaded})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", type=int)
    parser.add_argument("--extract", action="store_true")
    parser.add_argument("--groups", type=int, default=2000)
    parser.add_argument("--max-mib", type=int, default=1024)
    args = parser.parse_args()
    RangeReader.byte_limit = args.max_mib * 1024 * 1024
    if args.extract:
        extract(args.groups)
    else:
        inspect(args.inspect or 1)
