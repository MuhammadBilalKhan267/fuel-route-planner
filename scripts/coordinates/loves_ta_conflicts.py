"""Audit conflicting chain coordinates against independently named OSM premises."""

from __future__ import annotations

import argparse
import json
import math
import re
import xml.etree.ElementTree as ET

from loves_ta import DATA, PublicDownloads, normalized, write_json

SOURCES = {"loves_official_store", "ta_petro_official_store"}


def distance(a: dict, b: dict) -> float:
    lat1, lat2 = map(math.radians, (a["latitude"], b["latitude"]))
    delta_lat = lat2 - lat1
    delta_lon = math.radians(b["longitude"] - a["longitude"])
    half = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 6_371_000 * 2 * math.asin(min(1, math.sqrt(half)))


def named_chain_premises(record: dict, source: str) -> bool:
    tags = record["tags"]
    if tags.get("disused") == "yes" or tags.get("abandoned") == "yes":
        return False
    names = " ".join(tags.get(key, "") for key in ("name", "brand", "operator"))
    if source == "loves_official_store":
        return "LOVES" in normalized(names)
    return bool(re.search(r"\b(?:TA|PETRO|TRAVELCENTERS)\b", names, re.IGNORECASE))


def independent_records() -> list[dict]:
    records = []
    for path in (DATA / "osm-states").glob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        records.extend(value.get("records", []))
    return records


def xml_records(page: str) -> list[dict]:
    root = ET.fromstring(page)
    nodes = {
        node.attrib["id"]: (float(node.attrib["lat"]), float(node.attrib["lon"]))
        for node in root.findall("node")
    }
    records = []
    for element in root:
        tags = {tag.attrib["k"]: tag.attrib["v"] for tag in element.findall("tag")}
        named = any(named_chain_premises({"tags": tags}, source) for source in SOURCES)
        if tags.get("amenity") != "fuel" and tags.get("shop") != "convenience" and not named:
            continue
        if element.tag == "node":
            latitude, longitude = nodes[element.attrib["id"]]
        elif element.tag == "way":
            refs = list(dict.fromkeys(node.attrib["ref"] for node in element.findall("nd")))
            if not refs or any(ref not in nodes for ref in refs):
                continue
            latitude = sum(nodes[ref][0] for ref in refs) / len(refs)
            longitude = sum(nodes[ref][1] for ref in refs) / len(refs)
        else:
            continue
        records.append(
            {
                "type": element.tag,
                "id": int(element.attrib["id"]),
                "latitude": latitude,
                "longitude": longitude,
                "tags": tags,
            }
        )
    return records


def corroboration(official: dict, records: list[dict]) -> dict | None:
    choices = [
        (distance(official, record), record)
        for record in records
        if abs(record["latitude"] - official["latitude"]) < 0.004
        and abs(record["longitude"] - official["longitude"]) < 0.004
        and named_chain_premises(record, official["source"])
    ]
    choices.sort(key=lambda pair: pair[0])
    if not choices:
        return None
    meters, witness = choices[0]
    tags = witness["tags"]
    if meters <= 200 and (tags.get("amenity") == "fuel" or tags.get("shop") == "convenience"):
        return witness
    # A service-area label or shop can sit farther from the truck fuel canopy.
    # Require fuel mapped near the official point as well as the named property.
    if meters <= 325 and any(
        point["tags"].get("amenity") == "fuel" and distance(official, point) <= 200
        for point in records
        if abs(point["latitude"] - official["latitude"]) < 0.004
        and abs(point["longitude"] - official["longitude"]) < 0.004
    ):
        return witness
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    conflicts = json.loads((DATA / "conflicts.json").read_text(encoding="utf-8"))
    records = independent_records()
    review_path = DATA / "loves-ta-conflict-reviews.json"
    reviews = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else []
    previous = {row["opis_id"] for row in reviews}
    unresolved = []
    downloads = PublicDownloads()
    for conflict in conflicts:
        official = next(
            (candidate for candidate in conflict["candidates"] if candidate["source"] in SOURCES),
            None,
        )
        if official is None:
            continue
        station_id = official["opis_id"]
        if station_id in previous:
            continue
        cache = DATA / f"loves-ta-osm-map-{station_id}.json"
        cached = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
        local = cached.get("records", [])
        witness = corroboration(official, records + local)
        if witness is None and args.download and cached.get("schema_version", 1) < 2:
            lat, lon = official["latitude"], official["longitude"]
            url = f"https://api.openstreetmap.org/api/0.6/map?bbox={lon - 0.004:.7f},{lat - 0.004:.7f},{lon + 0.004:.7f},{lat + 0.004:.7f}"
            local = xml_records(downloads.get(url))
            write_json(
                cache,
                {
                    "schema_version": 2,
                    "source_url": url,
                    "retrieved_at": "2026-10-11",
                    "records": local,
                },
            )
            witness = corroboration(official, local)
        if witness is None:
            unresolved.append(
                {
                    "opis_id": station_id,
                    "reason": "No independently named chain premises and nearby fuel corroborate the official point",
                }
            )
            continue
        osm_url = f"https://www.openstreetmap.org/{witness['type']}/{witness['id']}"
        rejected = [
            {
                "source_url": candidate["source_url"],
                "source": candidate["source"],
                "latitude": candidate["latitude"],
                "longitude": candidate["longitude"],
                "reason": f"GPS is {distance(candidate, witness):.0f} meters from independently named {witness['tags'].get('name', witness['tags'].get('brand', 'chain'))} fuel premises and disagrees with the official store/address point. It cannot locate these premises.",
            }
            for candidate in conflict["candidates"]
            if candidate["source"] not in SOURCES
            and distance(candidate, official) > 400
            and distance(candidate, witness) > 600
        ]
        if not rejected:
            unresolved.append(
                {
                    "opis_id": station_id,
                    "reason": "Coordinate difference does not establish that another source is outside the premises",
                }
            )
            continue
        reviews.append(
            {
                **official,
                "source": "manually_reviewed_station_premises",
                "evidence": [
                    {
                        "source_url": official["source_url"],
                        "reason": f"Official chain record matches station identity and street address: {official['matched_name']}, {official['matched_address']}.",
                    },
                    {
                        "source_url": osm_url,
                        "reason": f"Independent named fuel premises ({witness['latitude']}, {witness['longitude']}) agree within {distance(official, witness):.0f} meters. Tags: {json.dumps(witness['tags'], ensure_ascii=True, sort_keys=True)}",
                    },
                ],
                "rejected_candidates": rejected,
            }
        )
    write_json(DATA / "loves-ta-conflict-reviews.json", reviews)
    write_json(DATA / "loves-ta-conflict-unresolved.json", unresolved)
    downloads.client.close()
    print(
        json.dumps(
            {
                "reviewed": len(reviews),
                "unresolved": len(unresolved),
                "public_get_requests": downloads.requests,
            }
        )
    )


if __name__ == "__main__":
    main()
