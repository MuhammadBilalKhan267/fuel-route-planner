"""Extract station POIs from state maps; run with uv run --group data.

Downloaded PBF files are temporary. Compact POIs and source metadata persist.
"""

import argparse
import json
import shutil
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import httpx
import osmium
from shapely.geometry import Polygon

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / "data/coordinate-sources" / "osm-states"
DOWNLOADS = ROOT / ".cache" / "osm-downloads"
INDEX_URL = "https://download.geofabrik.de/north-america/us.html"


class DownloadLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = {}

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get("href", "")
        if tag == "a" and href.endswith("-latest.osm.pbf"):
            name = Path(href).name.removesuffix("-latest.osm.pbf")
            if name != "us":
                self.links[name] = urljoin(INDEX_URL, href)


def relevant(tags):
    return (
        tags.get("amenity") == "fuel"
        or tags.get("shop") == "convenience"
        or tags.get("highway") == "services"
    )


class Stations(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.records = []
        self.invalid_geometries = 0

    def save(self, item, kind, longitude, latitude):
        self.records.append(
            {
                "type": kind,
                "id": item.id,
                "version": item.version,
                "longitude": longitude,
                "latitude": latitude,
                "tags": {
                    tag.k: tag.v
                    for tag in item.tags
                    if tag.k not in {"phone", "contact:phone", "fax", "contact:email", "email"}
                },
            }
        )

    def node(self, item):
        if relevant(item.tags) and item.location.valid():
            self.save(item, "node", item.location.lon, item.location.lat)

    def way(self, item):
        if not relevant(item.tags):
            return
        try:
            points = [(node.lon, node.lat) for node in item.nodes]
            if len(points) < 3 or points[0] != points[-1]:
                self.invalid_geometries += 1
                return
            polygon = Polygon(points)
            if not polygon.is_valid:
                self.invalid_geometries += 1
                return
            point = polygon.representative_point()
            self.save(item, "way", point.x, point.y)
        except osmium.InvalidLocationError:
            self.invalid_geometries += 1


def extract(client, state, url):
    output = DIRECTORY / f"{state}.json"
    if output.exists():
        return
    pbf = DOWNLOADS / f"{state}.osm.pbf"
    if not pbf.exists():
        partial = pbf.with_suffix(".part")
        with client.stream("GET", url) as response:
            response.raise_for_status()
            size = int(response.headers.get("content-length", 0))
            if size + 300_000_000 > shutil.disk_usage(DOWNLOADS).free:
                raise RuntimeError(f"Insufficient temporary disk space for {state}.")
            downloaded = 0
            last_report = time.monotonic()
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if time.monotonic() - last_report >= 15:
                        print(f"{state}: downloaded {downloaded // 1_000_000} MB", flush=True)
                        last_report = time.monotonic()
            partial.replace(pbf)
    print(f"{state}: extracting station POIs", flush=True)
    handler = Stations()
    handler.apply_file(
        str(pbf),
        locations=True,
        idx="flex_mem",
        filters=[
            osmium.filter.TagFilter(
                ("amenity", "fuel"), ("shop", "convenience"), ("highway", "services")
            )
        ],
    )
    payload = {
        "records": handler.records,
        "acquisition": {
            "source": url,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "license": "OpenStreetMap ODbL",
            "invalid_geometries": handler.invalid_geometries,
        },
    }
    pending = output.with_suffix(".part")
    with pending.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    pending.replace(output)
    if pbf.resolve().parent != DOWNLOADS.resolve():
        raise RuntimeError("Temporary map path escaped its download directory.")
    pbf.unlink()
    print(f"{state}: saved {len(handler.records)} POIs; temporary map removed", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "states", nargs="*", help="Geofabrik state names; empty downloads all states."
    )
    args = parser.parse_args()
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    index_path = DIRECTORY / "download-index.html"
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        if not index_path.exists():
            response = client.get(INDEX_URL)
            response.raise_for_status()
            index_path.write_text(response.text, encoding="utf-8")
        links = DownloadLinks()
        links.feed(index_path.read_text(encoding="utf-8"))
        states = args.states or list(links.links)
        if set(states) - links.links.keys():
            raise ValueError("A requested state was not present in the official download index.")
        for state in states:
            extract(client, state, links.links[state])


if __name__ == "__main__":
    main()
