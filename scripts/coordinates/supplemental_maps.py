"""Cache small public map extracts for individual coordinate reviews."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from loves_ta import DATA, PublicDownloads, write_json
from loves_ta_conflicts import xml_records


def main() -> None:
    records = json.loads((DATA / "supplemental-source-records.json").read_text(encoding="utf-8"))
    unresolved = json.loads((DATA / "supplemental-unresolved.json").read_text(encoding="utf-8"))
    points = {}
    for row in unresolved:
        station_id = int(row["OPIS Truckstop ID"])
        if (
            station_id
            in {
                68403,
                70460,
                71923,
                72012,
                72016,
                72445,
                72447,
                72755,
                72761,
                63704,
                64662,
            }
            and len(row["possible"]) == 1
        ):
            points[station_id] = row["possible"][0]
    for station_id, address in ((64492, "Shadeland"), (64496, "Harrison"), (64509, "Boden")):
        points[station_id] = next(
            record
            for record in records
            if record["brand"] == "QuickFuel" and address in record["address"]
        )
    points[65398] = {"latitude": 41.413588, "longitude": -81.807244}
    points[64799] = next(
        record
        for record in records
        if record["brand"] == "QuickFuel" and "Trailer" in record["address"]
    )
    downloads = PublicDownloads()
    for station_id, record in points.items():
        path = DATA / f"supplemental-osm-map-{station_id}.json"
        if path.exists():
            continue
        lat, lon = record["latitude"], record["longitude"]
        url = f"https://api.openstreetmap.org/api/0.6/map?bbox={lon - 0.003:.7f},{lat - 0.003:.7f},{lon + 0.003:.7f},{lat + 0.003:.7f}"
        page = downloads.get(url)
        root = ET.fromstring(page)
        tags = [
            {
                "type": element.tag,
                "id": element.attrib["id"],
                "tags": {tag.attrib["k"]: tag.attrib["v"] for tag in element.findall("tag")},
            }
            for element in root
            if any(
                tag.attrib["k"] in {"name", "brand", "addr:housenumber", "addr:street"}
                for tag in element.findall("tag")
            )
        ]
        write_json(
            path,
            {
                "source_url": url,
                "retrieved_at": "2026-10-11",
                "records": xml_records(page),
                "named_features": tags,
            },
        )
    downloads.client.close()
    print(json.dumps({"maps": len(points), "requests": downloads.requests}))


if __name__ == "__main__":
    main()
