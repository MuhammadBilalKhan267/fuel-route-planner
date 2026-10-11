"""Join cached Overture POIs to exact published station street identities.

Outputs are review proposals. Native POI coordinates are not certified premises.
"""

import json
import math
import re
from collections import defaultdict
from itertools import chain

from shapely import make_valid
from shapely.geometry import Point, shape

from scripts.coordinates.match_osm import identity, stations
from scripts.coordinates.matching import brand, normalized, station_name_keys, store_number
from scripts.coordinates.places import inside_named_place, place_boundaries
from scripts.coordinates.premises import numeric_street, published_identities
from scripts.coordinates.regional import ROOT, SOURCES, normalized_address


def sevenfleet_addresses(records):
    """Retain direct current store IDs as address identities, without their pins."""
    for record in records:
        if record.get("Status") != "Open":
            continue
        title = record.get("Store: Store Name", "")
        match = re.fullmatch(r"(7-Eleven|Stripes|Speedway)[ -]+(\d+)", title, re.IGNORECASE)
        number = record.get("Store #", "")
        if not match or not number.isdecimal() or int(match[2]) != int(number):
            continue
        yield {
            "name": f"{match[1]} #{int(number)}",
            "street": record["Address"],
            "city": record["City"],
            "state": record["State"],
            "source_url": "https://7fleetnetwork.com/wp21/wp-content/uploads/2026/08/7Fleet_Locations_2026-07-08b.csv",
            "external_id": f"7fleet:{number}",
            "native_store_name": title,
            "native_store_number": number,
            "phone": record.get("Phone", ""),
            "fuel_brand": record.get("Brand (Gas)", ""),
            "coordinate_verified": False,
        }


def native_name_matches(row, published, native_name):
    """Require the same distinctive identity or the same brand at an exact street."""
    actual_number = store_number(native_name)
    actual_brand = brand(native_name)
    expected = [published["name"], *row["names"]]
    expected_numbers = {store_number(name) for name in expected} - {None}
    expected_brands = {brand(name) for name in expected} - {None}
    if actual_number and expected_numbers and actual_number not in expected_numbers:
        return False
    if actual_brand and actual_brand in expected_brands:
        return True
    # The primary operator may explicitly name a separate pump brand at this
    # exact store address. Preserve that fact; do not infer a retailer rebrand.
    published_fuel_brand = brand(published.get("fuel_brand", ""))
    if actual_brand and actual_brand == published_fuel_brand:
        return True
    if actual_brand or expected_brands:
        return False
    actual_keys = station_name_keys(native_name, row["City"])
    expected_keys = {key for name in expected for key in station_name_keys(name, row["City"])}
    if any(len(key) >= 7 and key in expected_keys for key in actual_keys):
        return True
    # A short proprietor name such as Buch's remains distinctive when the full
    # published business name also agrees. Generic "Truck Stop" strips to empty.
    full_name = normalized(native_name)
    return (
        len(full_name) >= 7
        and any(len(key) >= 4 and key in expected_keys for key in actual_keys)
        and any(full_name == normalized(name) for name in expected)
    )


def qualifying_addresses(row, published, poi, inside_city):
    if not numeric_street(published["street"]):
        return []
    lat, lon = poi.get("latitude"), poi.get("longitude")
    if not all(isinstance(value, (float, int)) and math.isfinite(value) for value in (lat, lon)):
        return []
    if not native_name_matches(row, published, poi["name"]):
        return []
    accepted = []
    for address in poi.get("addresses") or []:
        if address.get("country") != "US" or address.get("region") != row["State"]:
            continue
        street = address.get("freeform", "")
        if not numeric_street(street) or normalized_address(street) != normalized_address(
            published["street"]
        ):
            continue
        city = address.get("locality", "")
        if city and normalized(city) != normalized(row["City"]):
            continue
        if not city and not inside_city(row, poi):
            continue
        accepted.append(address)
    return accepted


def main():
    rows = stations()
    eligible = set()
    for filename in ("unresolved.json", "conflicts.json"):
        eligible.update(
            item["opis_id"] for item in json.loads((SOURCES / filename).read_text(encoding="utf-8"))
        )
    # Root owns the current Louisiana recovery and its operator rebrands.
    eligible = {identifier for identifier in eligible if rows[identifier]["State"] != "LA"}
    by_city = defaultdict(list)
    for identifier in eligible:
        row = rows[identifier]
        by_city[(row["State"], normalized(row["City"]))].append((identifier, row))

    places = place_boundaries(rows)
    identities = defaultdict(list)
    sevenfleet = list(
        sevenfleet_addresses(
            json.loads((SOURCES / "regional-7fleet-locations.json").read_text(encoding="utf-8"))
        )
    )
    (SOURCES / "regional-7fleet-address-identities.json").write_text(
        json.dumps(sevenfleet, indent=2) + "\n", encoding="utf-8"
    )
    for record in chain(published_identities(rows, places), sevenfleet):
        if not numeric_street(record.get("street", "")):
            continue
        for identifier, row in by_city.get((record["state"], normalized(record["city"])), []):
            if identity(row, {"tags": {"name": record["name"], "addr:city": record["city"]}}):
                identities[(record["state"], normalized_address(record["street"]))].append(
                    (identifier, record)
                )

    states = {
        feature["properties"]["STUSAB"]: make_valid(shape(feature["geometry"]))
        for feature in json.loads((ROOT / "trips/data/us-states.geojson").read_text())["features"]
    }
    proposals = {}
    files = sorted(SOURCES.glob("regional-overture-pois-*.json"))
    raw_count = 0
    for path in files:
        points = json.loads(path.read_text(encoding="utf-8"))
        raw_count += len(points)
        for poi in points:
            for address in poi.get("addresses") or []:
                street, state = address.get("freeform", ""), address.get("region", "")
                if address.get("country") != "US" or not numeric_street(street):
                    continue
                for identifier, published in identities.get(
                    (state, normalized_address(street)), []
                ):
                    row = rows[identifier]
                    matched = qualifying_addresses(
                        row,
                        published,
                        poi,
                        lambda target, point: inside_named_place(target, point, places),
                    )
                    if not matched or not states[state].covers(
                        Point(poi["longitude"], poi["latitude"])
                    ):
                        continue
                    key = (identifier, poi["external_id"])
                    if key not in proposals:
                        proposals[key] = {
                            "opis_id": identifier,
                            "original_names": row["names"],
                            "original_address": row["Address"],
                            "original_city": row["City"],
                            "original_state": row["State"],
                            "native_poi": poi,
                            "native_matched_addresses": matched,
                            "identity_sources": [],
                            "cache": path.name,
                            "coordinate_verified": False,
                            "review_required": "Bind this exact street identity to actual mapped fuel equipment or manually checked aerial premises before routing.",
                        }
                    source = {
                        key: published[key]
                        for key in ("name", "street", "city", "state", "source_url", "external_id")
                    }
                    if published.get("fuel_brand"):
                        source["fuel_brand"] = published["fuel_brand"]
                    if source not in proposals[key]["identity_sources"]:
                        proposals[key]["identity_sources"].append(source)
    results = sorted(
        proposals.values(), key=lambda item: (item["opis_id"], item["native_poi"]["external_id"])
    )
    (SOURCES / "regional-overture-identity-proposals.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8"
    )
    print(
        {
            "completed_files": len(files),
            "raw_pois": raw_count,
            "strict_identity_proposals": len(results),
            "station_ids": len({item["opis_id"] for item in results}),
        }
    )


if __name__ == "__main__":
    main()
