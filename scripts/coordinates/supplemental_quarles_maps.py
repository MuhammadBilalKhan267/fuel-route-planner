"""Cache small OSM property extracts around recovered Quarles street anchors."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from loves_ta import DATA, PublicDownloads, write_json


def mapped_records(page: str) -> list[dict]:
    root = ET.fromstring(page)
    nodes = {
        element.attrib["id"]: [float(element.attrib["lat"]), float(element.attrib["lon"])]
        for element in root.findall("node")
    }
    records = []
    for element in root:
        tags = {tag.attrib["k"]: tag.attrib["v"] for tag in element.findall("tag")}
        if not tags or element.tag not in {"node", "way"}:
            continue
        if element.tag == "node":
            coordinates = [nodes[element.attrib["id"]]]
        else:
            refs = list(dict.fromkeys(node.attrib["ref"] for node in element.findall("nd")))
            if not refs or any(ref not in nodes for ref in refs):
                continue
            coordinates = [nodes[ref] for ref in refs]
        records.append(
            {
                "type": element.tag,
                "id": int(element.attrib["id"]),
                "latitude": sum(point[0] for point in coordinates) / len(coordinates),
                "longitude": sum(point[1] for point in coordinates) / len(coordinates),
                "tags": tags,
                "coordinates": coordinates,
            }
        )
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    anchors = json.loads(
        (DATA / "supplemental-quarles-census-anchors.json").read_text(encoding="utf-8")
    )
    accepted = {
        row["opis_id"]
        for row in json.loads(
            (DATA / "supplemental-bridge-candidates.json").read_text(encoding="utf-8")
        )
    }
    downloads = PublicDownloads()
    cached, requests = 0, 0
    for anchor in anchors:
        if anchor["match_status"] != "Match" or anchor["opis_id"] in accepted:
            continue
        path = DATA / f"supplemental-quarles-map-{anchor['opis_id']}.json"
        if path.exists():
            cached += 1
            continue
        if not args.download or requests >= args.limit:
            continue
        lat, lon = anchor["latitude"], anchor["longitude"]
        url = f"https://api.openstreetmap.org/api/0.6/map?bbox={lon - 0.0025:.7f},{lat - 0.0025:.7f},{lon + 0.0025:.7f},{lat + 0.0025:.7f}"
        page = downloads.get(url)
        write_json(
            path,
            {
                "source_url": url,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "anchor": anchor,
                "records": mapped_records(page),
            },
        )
        requests += 1
        if requests % 10 == 0:
            print({"new_maps": requests, "cached_maps": cached}, flush=True)
    downloads.client.close()
    print({"new_maps": requests, "cached_maps": cached})


if __name__ == "__main__":
    main()
