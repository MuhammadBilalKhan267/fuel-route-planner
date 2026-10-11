"""Bind published station street addresses to independently mapped fuel premises."""

import json
import math
import re
from collections import defaultdict
from pathlib import Path

from scripts.coordinates.match_osm import distinct_sites, identity, load_pois, same_place, stations
from scripts.coordinates.matching import brand, normalized, station_name_keys, store_number
from scripts.coordinates.places import SOURCE_URL, inside_named_place, place_boundaries
from scripts.coordinates.regional import normalized_address

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "data/coordinate-sources"


def numeric_street(value):
    return bool(re.match(r"^\s*\d[\dA-Z-]*(?:\s+|$)", value or "", re.IGNORECASE))


def valid_geometry(feature):
    geometry = feature.get("geometry")
    if not geometry or geometry.get("type") != "Point":
        return False
    coordinates = geometry.get("coordinates", [])
    return len(coordinates) >= 2 and all(
        isinstance(value, (int, float)) and math.isfinite(value) for value in coordinates[:2]
    )


def epa_address_identities(records, rows, places):
    by_state = defaultdict(dict)
    for row in rows.values():
        by_state[row["State"]].setdefault(normalized(row["City"]), []).append(row)
    for record in records:
        item = {
            key: record[key]
            for key in ("name", "city", "state", "street", "source_url", "external_id")
        }
        if item["city"]:
            yield item
            continue
        if not numeric_street(item["street"]) or not valid_geometry(
            {
                "geometry": {
                    "type": "Point",
                    "coordinates": [record.get("longitude"), record.get("latitude")],
                }
            }
        ):
            continue
        for city_rows in by_state[item["state"]].values():
            row = city_rows[0]
            if not inside_named_place(row, record, places):
                continue
            if any(
                identity(target, {"tags": {"name": item["name"], "addr:city": row["City"]}})
                for target in city_rows
            ):
                yield {
                    **item,
                    "city": row["City"],
                    "city_qualification": "inside_named_census_place",
                }


def published_identities(rows=None, places=None):
    rows = stations() if rows is None else rows
    epa_path = SOURCES / "epa-ust-addresses.json"
    if epa_path.exists():
        # Historical facility addresses establish identity only. Neither the
        # EPA geocode nor its old tank status certifies a current station.
        yield from epa_address_identities(
            json.loads(epa_path.read_text()),
            rows,
            place_boundaries(rows) if places is None else places,
        )
    for record in json.loads((SOURCES / "fuelman-stations.json").read_text()):
        yield {
            "name": record["name"],
            "city": record["city"],
            "state": record["stateCode"],
            "street": record["address1"],
            "source_url": record["source_url"],
            "external_id": str(record["id"]),
        }
    for record in json.loads((SOURCES / "supplemental-comdata-addresses.json").read_text()):
        yield {**record, "street": record["address"]}
    for record in json.loads((SOURCES / "regional-nrha-merchant-addresses.json").read_text()):
        yield {
            **record,
            "street": record["address"],
            "external_id": f"nrha:{record['page']}:{record['name']}:{record['address']}",
        }
    natso_path = SOURCES / "regional-secondary-natso-stations.json"
    if natso_path.exists():
        for record in json.loads(natso_path.read_text()):
            parts = [part.strip() for part in record["address"].split(",")]
            state_position = next(
                (
                    index
                    for index, part in enumerate(parts)
                    if re.fullmatch(r"[A-Z]{2}\s+\d{5}(?:-\d{4})?", part)
                ),
                None,
            )
            if state_position is None or state_position < 2:
                continue
            yield {
                "name": record["name"],
                "street": ", ".join(parts[: state_position - 1]),
                "city": parts[state_position - 1],
                "state": parts[state_position][:2],
                "source_url": record["source_url"],
                "external_id": f"natso:{record['name']}:{record['address']}",
            }
    for name in (
        "independent-identity-bigd-stations.json",
        "independent-identity-petes-stations.json",
        "independent-identity-faststop-addresses.json",
        "independent-identity-pwi-addresses.json",
    ):
        path = SOURCES / name
        if path.exists():
            for record in json.loads(path.read_text()):
                yield {**record, "street": record["address"]}
    path = SOURCES / "regional-7fleet-address-identities.json"
    if path.exists():
        for record in json.loads(path.read_text()):
            yield {
                key: record[key]
                for key in ("name", "street", "city", "state", "source_url", "external_id")
            }
    path = SOURCES / "snap-identities.json"
    if path.exists():
        for item in json.loads(path.read_text()):
            row = rows[item["opis_id"]]
            yield {
                "name": item["matched_name"],
                "city": row["City"],
                "state": row["State"],
                "street": item["matched_address"],
                "source_url": item["source_url"],
                "external_id": item["external_id"],
            }


def street_matches(row, record, points, anchors, places=None):
    matches = []
    for point in points:
        if not same_place(row, point, anchors):
            continue
        if (
            places is not None
            and not point["tags"].get("addr:city")
            and not inside_named_place(row, point, places)
        ):
            continue
        actual = {brand(point["tags"].get(key, "")) for key in ("name", "brand", "operator")} - {
            None
        }
        expected = {brand(name) for name in row["names"]} - {None}
        # A different named retail operator at the same street may be a rebrand;
        # preserve that case for review rather than silently transfer the price.
        retail = actual - {
            "shell",
            "exxon",
            "bp",
            "cenex",
            "chevron",
            "sinclair",
            "marathon",
            "sunoco",
            "texaco",
            "citgo",
            "amoco",
            "valero",
            "phillips66",
            "conoco",
            "union76",
            "arco",
        }
        if expected and retail and not expected & actual:
            continue
        matches.append({**point, "identity_record": record})
    return matches


def addressed_premises():
    for state, points in load_pois().items():
        for point in points:
            tags = point["tags"]
            street = " ".join(
                tags.get(key, "") for key in ("addr:housenumber", "addr:street")
            ).strip()
            yield {
                **point,
                "state": state,
                "street": street,
                "source_url": f"https://www.openstreetmap.org/{point['type']}/{point['id']}",
                "external_id": f"{point['type']}/{point['id']}",
                "source": "osm_premises_with_published_address",
            }
    for folder in (SOURCES / "fuel-locators", SOURCES / "minor-chains"):
        for path in sorted(folder.glob("*.json")):
            data = json.loads(path.read_text())
            for feature in data.get("features", []):
                if not valid_geometry(feature):
                    continue
                tags = feature["properties"]
                if tags.get("addr:country") != "US":
                    continue
                if tags.get("amenity") != "fuel" and tags.get("shop") != "convenience":
                    continue
                full = tags.get("addr:full", "").split(",")
                city = tags.get("addr:city", full[-2].strip() if len(full) >= 3 else "")
                street = (
                    tags.get("addr:street_address")
                    or " ".join(
                        tags.get(key, "") for key in ("addr:housenumber", "addr:street")
                    ).strip()
                    or (full[0] if len(full) >= 3 else "")
                )
                yield {
                    "longitude": feature["geometry"]["coordinates"][0],
                    "latitude": feature["geometry"]["coordinates"][1],
                    "tags": {
                        "name": tags.get("operator") or tags.get("name", ""),
                        "brand": tags.get("brand", ""),
                        "addr:city": city,
                    },
                    "state": tags.get("addr:state", ""),
                    "street": street,
                    "source_url": tags["@source_uri"],
                    "external_id": tags.get("ref", feature["id"]),
                    "source": "operator_premises_with_published_address",
                }
    flyers_path = SOURCES / "independent-identity-flyers-stations.json"
    if flyers_path.exists():
        for station in json.loads(flyers_path.read_text()):
            yield {
                "longitude": station["longitude"],
                "latitude": station["latitude"],
                "tags": {"name": station["name"], "addr:city": station["city"]},
                "state": station["state"],
                "street": station["address"],
                "source_url": station["source_url"],
                "external_id": station["external_id"],
                "source": "operator_premises_with_published_address",
            }
    refuel = json.loads((SOURCES / "supplemental-refuel-official-stores.json").read_text())
    for station in refuel["response"]:
        if "office" in station.get("description", "").casefold():
            continue
        try:
            latitude, longitude = float(station["lat"]), float(station["lng"])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(latitude) or not math.isfinite(longitude):
            continue
        yield {
            "longitude": longitude,
            "latitude": latitude,
            "tags": {"name": station["title"], "addr:city": station["city"]},
            "state": station["state"],
            "street": station["street"],
            "source_url": refuel["source_url"],
            "external_id": f"refuel:{station['id']}",
            "source": "operator_premises_with_published_address",
        }
    afdc = json.loads((SOURCES / "supplemental-afdc-official-stations.json").read_text())
    for station in afdc["response"]["fuel_stations"]:
        if station["status_code"] != "E" or station["geocode_status"] != "GPS":
            continue
        yield {
            "longitude": station["longitude"],
            "latitude": station["latitude"],
            "tags": {"name": station["station_name"], "addr:city": station["city"]},
            "state": station["state"],
            "street": station["street_address"],
            "source_url": f"https://afdc.energy.gov/stations#/station/{station['id']}",
            "external_id": f"afdc:{station['id']}",
            "source": "federal_gps_premises_with_published_address",
        }


def main():
    rows = stations()
    places = place_boundaries(rows)
    anchors = {
        normalized(item["city"]) + "|" + item["state"]: item
        for item in json.loads((SOURCES / "city-search-anchors.json").read_text()).values()
    }
    by_street = defaultdict(list)
    for point in addressed_premises():
        if numeric_street(point["street"]):
            by_street[(point["state"], normalized_address(point["street"]))].append(point)
    by_name, by_number = defaultdict(list), defaultdict(list)
    for record in published_identities(rows, places):
        if not numeric_street(record["street"]):
            continue
        place = record["state"], normalized(record["city"])
        for key in station_name_keys(record["name"], record["city"]):
            by_name[(*place, key)].append(record)
        by_number[(*place, brand(record["name"]), store_number(record["name"]))].append(record)
    accepted, ambiguous = [], []
    for identifier, row in rows.items():
        place = row["State"], normalized(row["City"])
        records = {}
        for name in row["names"]:
            named = [
                record
                for key in station_name_keys(name, row["City"])
                for record in by_name[(*place, key)]
            ]
            for record in named + by_number[(*place, brand(name), store_number(name))]:
                records[(record["source_url"], record["external_id"])] = record
        matches = []
        for record in records.values():
            if not identity(row, {"tags": {"name": record["name"], "addr:city": record["city"]}}):
                continue
            matches.extend(
                street_matches(
                    row,
                    record,
                    by_street[(row["State"], normalized_address(record["street"]))],
                    anchors,
                    places,
                )
            )
        sites = distinct_sites(matches)
        if len(sites) != 1:
            if sites:
                ambiguous.append({"opis_id": identifier, "candidates": sites})
            continue
        point = sites[0]
        record = point["identity_record"]
        accepted.append(
            {
                "opis_id": identifier,
                "latitude": point["latitude"],
                "longitude": point["longitude"],
                "source": point["source"],
                "source_url": point["source_url"],
                "external_id": point["external_id"],
                "matched_name": point["tags"].get("name", ""),
                "matched_address": record["street"],
                "evidence": [
                    {
                        "source_url": record["source_url"],
                        "external_id": record["external_id"],
                        "reason": (
                            "Published station name/store ID and state match the CSV. The source has no city; its search point lies inside the supplied city's Census boundary. Its numeric street must independently match mapped station premises."
                            if record.get("city_qualification")
                            else "Published station name/store ID, city and state match the CSV and supply this numeric street address."
                        ),
                    },
                    "exact_numeric_street_address_at_mapped_fuel_or_convenience_premises",
                    "same_state",
                ]
                + (
                    [
                        {
                            "source_url": SOURCE_URL,
                            "reason": "The historical facility search point is inside this named city boundary; its coordinates were not imported.",
                        }
                    ]
                    if record.get("city_qualification")
                    else []
                ),
            }
        )
    for name, data in (("premises-candidates", accepted), ("premises-ambiguous", ambiguous)):
        (SOURCES / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(
        {
            "addressed_premises": sum(len(value) for value in by_street.values()),
            "accepted": len(accepted),
            "ambiguous": len(ambiguous),
        }
    )


if __name__ == "__main__":
    main()
