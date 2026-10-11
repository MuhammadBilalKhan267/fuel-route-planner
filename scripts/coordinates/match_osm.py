"""Conservative station-identity matches against locally extracted OSM POIs."""

import csv
import json
import math
from collections import defaultdict
from pathlib import Path

from pyproj import Geod
from shapely import make_valid, prepare
from shapely.geometry import Point, shape

from scripts.coordinates.matching import brand, normalized, station_name_keys, store_number

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "data/coordinate-sources"
GEOD = Geod(ellps="WGS84")


def stations():
    rows = {}
    with (ROOT / "fuel-prices-for-be-assessment.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            identifier = int(row["OPIS Truckstop ID"])
            if identifier not in rows:
                rows[identifier] = {**row, "names": []}
            rows[identifier]["names"].append(row["Truckstop Name"])
    return rows


def same_place(row, point, anchors):
    actual_city = point["tags"].get("addr:city", "")
    if actual_city:
        return "same_city" if normalized(actual_city) == normalized(row["City"]) else None
    anchor = anchors.get(normalized(row["City"]) + "|" + row["State"])
    if not anchor:
        return None
    distance = abs(
        GEOD.inv(anchor["longitude"], anchor["latitude"], point["longitude"], point["latitude"])[2]
    )
    return "near_city_search_anchor" if distance <= 32_000 else None


def identity(row, point):
    tags = point["tags"]
    names = [tags.get(key, "") for key in ("name", "official_name", "alt_name")]
    city = row.get("City", "")
    same_city = bool(city) and normalized(tags.get("addr:city", "")) == normalized(city)
    expected_numbers = {store_number(value) for value in row["names"]} - {None}
    minimum_name_length = 4 if same_city else 7
    source_names = {
        key for value in row["names"] for key in station_name_keys(value, city if same_city else "")
    }
    for name in names:
        keys = station_name_keys(name, city if same_city else "")
        if any(len(key) >= minimum_name_length and key in source_names for key in keys):
            # Generic chain names alone do not distinguish individual locations.
            chain = brand(name)
            if (
                chain is None
                or store_number(name) is not None
                or expected_numbers
                or any(len(key) >= len(chain) + 6 and key in source_names for key in keys)
            ):
                return "exact_station_name"
    expected_brands = {brand(value) for value in row["names"]} - {None}
    actual_brands = {brand(tags.get(key, "")) for key in ("name", "brand", "operator")} - {None}
    actual_numbers = {store_number(value) for value in names} - {None}
    for key in ("ref", "ref:store", "brand:ref"):
        value = tags.get(key, "")
        if value.isdecimal():
            actual_numbers.add(str(int(value)))
    if expected_brands & actual_brands and expected_numbers & actual_numbers:
        return "same_brand_and_store_number"
    return None


def distinct_sites(points):
    sites = []
    for point in points:
        if all(
            abs(
                GEOD.inv(
                    point["longitude"], point["latitude"], other["longitude"], other["latitude"]
                )[2]
            )
            > 120
            for other in sites
        ):
            sites.append(point)
    return sites


def load_pois():
    features = json.loads((ROOT / "trips/data/us-states.geojson").read_text())["features"]
    states = {
        feature["properties"]["STUSAB"]: make_valid(shape(feature["geometry"]))
        for feature in features
    }
    for boundary in states.values():
        prepare(boundary)
    by_state = defaultdict(list)
    point_ids = set()
    for path in (SOURCES / "osm-states").glob("*.json"):
        for point in json.loads(path.read_text(encoding="utf-8"))["records"]:
            identifier = (point["type"], point["id"])
            if identifier in point_ids:
                continue
            point_ids.add(identifier)
            longitude, latitude = point["longitude"], point["latitude"]
            if not math.isfinite(longitude) or not math.isfinite(latitude):
                continue
            location = Point(longitude, latitude)
            for state, boundary in states.items():
                if boundary.covers(location):
                    by_state[state].append(point)
                    break
    return by_state


def main():
    from scripts.coordinates.places import SOURCE_URL, inside_named_place, place_boundaries

    rows = stations()
    places = place_boundaries(rows)
    by_state = load_pois()
    anchors = {
        normalized(item["city"]) + "|" + item["state"]: item
        for item in json.loads((SOURCES / "city-search-anchors.json").read_text()).values()
    }
    accepted, ambiguous, search_proposals = [], [], []
    indexed = {}
    for state, points in by_state.items():
        names, numbers = defaultdict(list), defaultdict(list)
        for point in points:
            tags = point["tags"]
            for key in ("name", "official_name", "alt_name"):
                if tags.get(key):
                    for name_key in station_name_keys(tags[key], tags.get("addr:city", "")):
                        names[name_key].append(point)
            point_brands = {brand(tags.get(key, "")) for key in ("name", "brand", "operator")} - {
                None
            }
            point_numbers = {
                store_number(tags.get(key, "")) for key in ("name", "official_name")
            } - {None}
            for key in ("ref", "ref:store", "brand:ref"):
                if tags.get(key, "").isdecimal():
                    point_numbers.add(str(int(tags[key])))
            for chain in point_brands:
                for number in point_numbers:
                    numbers[(chain, number)].append(point)
        indexed[state] = (names, numbers)
    for identifier, row in rows.items():
        if row["State"] not in indexed:
            continue
        names, numbers = indexed[row["State"]]
        candidates = {}
        for name in row["names"]:
            named = [
                point
                for key in station_name_keys(name, row["City"])
                for point in names.get(key, [])
            ]
            for point in named + numbers.get((brand(name), store_number(name)), []):
                candidates[(point["type"], point["id"])] = point
        matches = []
        for point in candidates.values():
            rule, place = identity(row, point), same_place(row, point, anchors)
            if rule and place:
                if place == "near_city_search_anchor" and rule != "same_brand_and_store_number":
                    if not inside_named_place(row, point, places):
                        search_proposals.append(
                            {
                                "opis_id": identifier,
                                "point": point,
                                "reason": "A city-radius name match is only a search proposal; no exact city or published street identity binds these premises.",
                            }
                        )
                        continue
                    place = "inside_named_census_place"
                matches.append(
                    {
                        **point,
                        "evidence": [rule, place, "same_state", "osm_station_poi"]
                        + (
                            [
                                {
                                    "source_url": SOURCE_URL,
                                    "reason": "Mapped named premises are inside the supplied city's published boundary.",
                                }
                            ]
                            if place == "inside_named_census_place"
                            else []
                        ),
                    }
                )
        sites = distinct_sites(matches)
        if len(sites) != 1:
            if sites:
                ambiguous.append({"opis_id": identifier, "candidates": sites})
            continue
        point = sites[0]
        accepted.append(
            {
                "opis_id": identifier,
                "latitude": point["latitude"],
                "longitude": point["longitude"],
                "source": "osm_station_poi",
                "source_url": f"https://www.openstreetmap.org/{point['type']}/{point['id']}",
                "external_id": f"{point['type']}/{point['id']}",
                "matched_name": point["tags"].get("name", ""),
                "matched_address": " ".join(
                    point["tags"].get(key, "")
                    for key in ("addr:housenumber", "addr:street", "addr:city", "addr:state")
                ),
                "evidence": point["evidence"],
                "original_names": row["names"],
                "original_address": row["Address"],
                "original_city": row["City"],
                "original_state": row["State"],
            }
        )
    (SOURCES / "osm-candidates.json").write_text(
        json.dumps(accepted, indent=2) + "\n", encoding="utf-8"
    )
    (SOURCES / "osm-ambiguous.json").write_text(
        json.dumps(ambiguous, indent=2) + "\n", encoding="utf-8"
    )
    (SOURCES / "osm-search-proposals.json").write_text(
        json.dumps(search_proposals, indent=2) + "\n", encoding="utf-8"
    )
    print(
        {
            "pois": sum(len(points) for points in by_state.values()),
            "matched_stations": len(accepted),
            "ambiguous_stations": len(ambiguous),
        }
    )


if __name__ == "__main__":
    main()
