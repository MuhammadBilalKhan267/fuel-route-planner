"""Match cached regional operator locations to published station identities."""

import html
import json
import math
import re
from collections import defaultdict
from pathlib import Path

from scripts.coordinates.match_osm import stations
from scripts.coordinates.matching import brand, normalized, station_name_keys, store_number
from scripts.coordinates.regional import normalized_address

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "data/coordinate-sources"
TARGETS = {
    "cubbys",
    "gasnwash",
    "townpump",
    "holiday",
    "irving",
    "thorntons",
    "pumpandpantry",
    "duckthru",
}


def read(name):
    return json.loads((SOURCES / name).read_text(encoding="utf-8"))


def write(name, records):
    (SOURCES / name).write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")


def station_brand(name):
    return "holiday" if normalized(name).startswith("holiday") else brand(name)


def number(name):
    value = store_number(name)
    if value:
        return value
    match = re.search(r"\b(\d+)\s*$", name)
    return str(int(match[1])) if match and station_brand(name) in TARGETS else None


def distance(first, second):
    latitude = math.radians((first["latitude"] + second["latitude"]) / 2)
    return 111000 * math.hypot(
        first["latitude"] - second["latitude"],
        (first["longitude"] - second["longitude"]) * math.cos(latitude),
    )


def geojson_points(filename, chain):
    for feature in read(filename)["features"]:
        tags = feature["properties"]
        if tags.get("addr:country") != "US":
            continue
        if tags.get("amenity") != "fuel" and chain != "duckthru":
            continue
        full = tags.get("addr:full", "").split(",")
        street = tags.get("addr:street_address", full[0].strip() if len(full) >= 3 else "")
        city = tags.get("addr:city", full[-2].strip() if len(full) >= 3 else "")
        state = tags.get("addr:state", "")
        if len(full) >= 3 and re.match(r"[A-Z]{2}\b", full[-1].strip()):
            state = full[-1].strip()[:2]
        if chain == "cubbys" and "cubby" not in tags.get("operator", "").lower():
            continue
        ref = str(tags.get("ref", feature["id"]))
        yield {
            "brand": chain,
            "store_number": str(int(ref)) if ref.isdigit() else None,
            "name": tags.get("operator") or tags.get("name", chain),
            "address": street,
            "city": city,
            "state": state,
            "latitude": feature["geometry"]["coordinates"][1],
            "longitude": feature["geometry"]["coordinates"][0],
            "source_url": tags.get("website") or tags["@source_uri"],
            "original_source_url": tags["@source_uri"],
            "external_id": f"{chain}:{ref}",
        }


def operator_points():
    points = []
    for filename, chain in [
        ("minor-chains/thorntons_us.json", "thorntons"),
        ("minor-chains/cenex.json", "cubbys"),
        ("regional-secondary-duck-feed.json", "duckthru"),
    ]:
        points.extend(geojson_points(filename, chain))
    state_codes = {
        "Maine": "ME",
        "Vermont": "VT",
        "Massachusetts": "MA",
        "New Hampshire": "NH",
        "Connecticut": "CT",
        "Rhode Island": "RI",
    }
    for feature in read("regional-secondary-irving-official.json")["features"]:
        record = feature["properties"]
        if not (
            record["isIrvingOil"]
            or record["isIrving24"]
            or (record["name"] == "Irving Oil" and record["diesel"])
        ):
            continue
        street, _, locality = record["address"].partition("<br/>")
        match = re.fullmatch(
            r"\s*(.+),\s*(Maine|Vermont|Massachusetts|New Hampshire|Connecticut|Rhode Island)\s+\d{5}(?:-\d{4})?\s*",
            locality,
        )
        if not match:
            continue
        # Mailing box text is not part of the physical street identity.
        street = re.split(r"\s+P\.?O\.?\s+BOX\b", street, flags=re.IGNORECASE)[0]
        points.append(
            {
                "brand": "irving",
                "store_number": None,
                "name": record["name"],
                "address": street,
                "city": match[1],
                "state": state_codes[match[2]],
                "latitude": float(record["lat"]),
                "longitude": float(record["long"]),
                "source_url": "https://www.irvingoil.com" + record["link"],
                "external_id": f"irving:{record['nid']}",
                "original_source_url": "https://www.irvingoil.com/location/geojson",
            }
        )
    for record in read("regional-secondary-pump-locations.json"):
        location = record["location"]
        if not all(location.get(key) for key in ("city", "state_short", "lat", "lng")):
            continue
        name = html.unescape(record["store_name"])
        points.append(
            {
                "brand": "pumpandpantry",
                "store_number": number(name),
                "name": name,
                "address": location.get("name") or record["address"].split(",")[0],
                "city": location["city"],
                "state": location["state_short"],
                "latitude": location["lat"],
                "longitude": location["lng"],
                "source_url": record["permalink"],
                "external_id": f"pumpandpantry:{number(name)}",
                "original_source_url": "https://pumpandpantry.com/wp-json/pump-pantry/v1/locations",
            }
        )
    circle = {
        str(record["id"]): record
        for filename in SOURCES.glob("regional-circlek-api-*.json")
        for record in json.loads(filename.read_text(encoding="utf-8"))
    }
    for identifier, record in circle.items():
        if not identifier.startswith(("2746", "2653")):
            continue
        address = record["address"]
        points.append(
            {
                "brand": "holiday",
                "store_number": identifier,
                "name": f"Holiday / Circle K #{identifier}",
                "address": address["street"],
                "city": address["city"],
                "state": address["state"],
                "latitude": record["latitude"],
                "longitude": record["longitude"],
                "source_url": "https://api.circlek.com/us/ngrp-store-locator/v1/stations",
                "external_id": f"circlek:{identifier}",
                "current_brand": record["brand"],
            }
        )
    return points


def address_identities():
    identities = read("regional-secondary-snap-addresses.json")
    for record in read("regional-nrha-merchant-addresses.json"):
        identities.append(
            {**record, "external_id": f"nrha:{record['page']}:{record['name']}", "current": False}
        )
    return [record for record in identities if station_brand(record["name"]) in TARGETS]


def exact_identity(row, record):
    brands = {station_brand(name) for name in row["names"]}
    if station_brand(record["name"]) not in brands:
        return False
    expected = {number(name) for name in row["names"]} - {None}
    if expected:
        return number(record["name"]) in expected
    keys = {key for name in row["names"] for key in station_name_keys(name, row["City"])}
    return bool(keys & station_name_keys(record["name"], row["City"]))


def main():
    points = operator_points()
    addresses = address_identities()
    by_place = defaultdict(list)
    by_street = defaultdict(list)
    for point in points:
        place = point["state"], normalized(point["city"])
        by_place[place].append(point)
        by_street[(*place, normalized_address(point["address"]))].append(point)
    accepted, unresolved = [], []
    for identifier, row in stations().items():
        brands = {station_brand(name) for name in row["names"]}
        if not brands & TARGETS:
            continue
        place = row["State"], normalized(row["City"])
        numbers = {number(name) for name in row["names"]} - {None}
        found = []
        for point in by_place[place]:
            if point["brand"] in brands and point["store_number"] in numbers:
                found.append(
                    {
                        **point,
                        "evidence": [
                            "exact_brand_store_number_city_state",
                            {"csv_name": row["names"], "csv_address": row["Address"]},
                        ],
                    }
                )
        for identity in addresses:
            if (identity["state"], normalized(identity["city"])) != place or not exact_identity(
                row, identity
            ):
                continue
            for point in by_street[(*place, normalized_address(identity["address"]))]:
                if point["brand"] not in brands:
                    continue
                found.append(
                    {
                        **point,
                        "evidence": [
                            {
                                "source_url": identity["source_url"],
                                "external_id": identity["external_id"],
                                "reason": "Exact station identity publishes the street matched to the current operator locator.",
                                "name": identity["name"],
                                "address": identity["address"],
                            },
                            "same_city_and_state",
                        ],
                    }
                )
        sites = []
        for point in found:
            if not any(
                normalized_address(point["address"]) == normalized_address(other["address"])
                and distance(point, other) <= 120
                for other in sites
            ):
                sites.append(point)
        if len(sites) != 1:
            unresolved.append(
                {
                    "opis_id": identifier,
                    "names": row["names"],
                    "city": row["City"],
                    "state": row["State"],
                    "reason": "No unique exact station identity and street bridge.",
                    "possible_sites": sites,
                }
            )
            continue
        point = sites[0]
        accepted.append(
            {
                "opis_id": identifier,
                "latitude": point["latitude"],
                "longitude": point["longitude"],
                "source": "official_regional_locator_with_published_station_identity",
                "source_url": point["source_url"],
                "external_id": point["external_id"],
                "matched_name": point["name"],
                "matched_address": point["address"],
                "evidence": point["evidence"]
                + [
                    {
                        "original_source_url": point.get(
                            "original_source_url", point["source_url"]
                        ),
                        "retrieved_on": "2026-10-11",
                    }
                ],
            }
        )
    write("regional-secondary-datasets.json", points)
    write("regional-secondary-candidates.json", accepted)
    write("regional-secondary-unresolved.json", unresolved)
    print(
        {
            "operator_records": len(points),
            "candidate_stations": len(accepted),
            "unresolved": len(unresolved),
        }
    )


if __name__ == "__main__":
    main()
