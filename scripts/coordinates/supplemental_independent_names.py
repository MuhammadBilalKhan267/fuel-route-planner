"""Find unresolved distinctive operators in cached named fuel and place records.

The output contains review leads only. Coordinates remain unverified until the
CSV identity, named fuel equipment, and original road or exit agree.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from scripts.coordinates.match_osm import GEOD, SOURCES, load_pois, stations
from scripts.coordinates.matching import brand, normalized, station_name_keys, store_number

DESCRIPTORS = re.compile(
    r"\b(?:gasoline|diesel|trucks?|autos?|pumps?|unleaded|fueling|services|"
    r"conv(?:enience)?|stores?|oils?)\b",
    re.IGNORECASE,
)
EXCLUSIONS = (
    "root-overture-premises-leads.json",
    "regional-exit-conflict-reviews.json",
    "regional-exit-fuel-proposals.json",
    "regional-exit-single-fuel-proposals.json",
    "regional-exit-nastc-identity-proposals.json",
    "regional-exit-brand-targets.json",
    "regional-exit-brand-route-proposals.json",
)


def name_keys(value: str, city: str = "") -> set[str]:
    """Drop equipment descriptions while preserving proprietor and branch digits."""
    return {key for key in station_name_keys(DESCRIPTORS.sub("", value), city) if len(key) >= 4}


def distinctive(value: str, city: str) -> bool:
    chain = brand(value)
    return any(
        key != normalized(city) and (chain is None or len(key) >= len(chain) + 6)
        for key in name_keys(value, city)
    )


def ids(value):
    """Read nested review IDs without assuming a proposal wrapper layout."""
    if isinstance(value, dict):
        if isinstance(value.get("opis_id"), int):
            yield value["opis_id"]
        for nested in value.values():
            yield from ids(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from ids(nested)


def meters(first, second):
    return abs(
        GEOD.inv(first["longitude"], first["latitude"], second["longitude"], second["latitude"])[2]
    )


def main() -> None:
    rows = stations()
    unresolved = {
        item["opis_id"]
        for item in json.loads((SOURCES / "unresolved.json").read_text(encoding="utf-8"))
    }
    excluded = {
        2817,
        5202,
        5477,
        60538,
        64352,
        65365,
        66456,
        66784,
        68581,
        70550,
        72303,
        72737,
        67312,
    }
    for filename in EXCLUSIONS:
        path = SOURCES / filename
        if path.exists():
            excluded.update(ids(json.loads(path.read_text(encoding="utf-8"))))
    for path in SOURCES.glob("root-*-conflict-reviews.json"):
        excluded.update(ids(json.loads(path.read_text(encoding="utf-8"))))
    eligible = unresolved - excluded
    indexed_rows = defaultdict(set)
    for identifier in eligible:
        row = rows[identifier]
        for name in set(row["names"]):
            if not distinctive(name, row["City"]):
                continue
            for key in name_keys(name, row["City"]):
                indexed_rows[(row["State"], normalized(row["City"]), key)].add(identifier)

    native_matches = defaultdict(dict)
    native_count = 0
    for path in sorted(SOURCES.glob("regional-overture-pois-*.json")):
        for point in json.loads(path.read_text(encoding="utf-8")):
            native_count += 1
            for address in point.get("addresses") or []:
                if address.get("country") != "US" or not address.get("locality"):
                    continue
                for key in name_keys(point["name"], address["locality"]):
                    identifiers = indexed_rows.get(
                        (address.get("region"), normalized(address["locality"]), key), []
                    )
                    for identifier in identifiers:
                        native_matches[identifier][point["external_id"]] = {
                            "point": point,
                            "cache": path.name,
                            "matched_name_key": key,
                        }

    named_fuel = defaultdict(dict)
    for state, points in load_pois().items():
        for point in points:
            if point["tags"].get("amenity") != "fuel":
                continue
            for field in ("name", "official_name", "alt_name", "operator"):
                for key in name_keys(point["tags"].get(field, "")):
                    named_fuel[(state, key)][(point["type"], point["id"])] = point
    exit_proposals = {
        r["opis_id"]: r
        for r in json.loads((SOURCES / "regional-exit-proposals.json").read_text(encoding="utf-8"))
    }
    proposals = []
    for identifier, matches in native_matches.items():
        row = rows[identifier]
        fuel = {}
        native = list(matches.values())
        for name in row["names"]:
            for key in name_keys(name, row["City"]):
                fuel.update(named_fuel.get((row["State"], key), {}))
        pairs = []
        wanted_numbers = {store_number(name) for name in row["names"]} - {None}
        for point in fuel.values():
            actual_city = point["tags"].get("addr:city", "")
            if actual_city and normalized(actual_city) != normalized(row["City"]):
                continue
            for match in native:
                native_point = match["point"]
                separation = meters(point, native_point)
                if separation > 150:
                    continue
                native_number = store_number(native_point["name"])
                fuel_numbers = {
                    store_number(point["tags"].get(field, ""))
                    for field in ("name", "official_name", "alt_name", "operator")
                } - {None}
                fuel_numbers.update(
                    str(int(value))
                    for field in ("ref", "ref:store", "brand:ref")
                    if (value := point["tags"].get(field, "")).isdecimal()
                )
                number_unconfirmed = bool(
                    wanted_numbers
                    and (native_number not in wanted_numbers or not wanted_numbers & fuel_numbers)
                )
                exit_evidence = []
                for candidate in exit_proposals.get(identifier, {}).get("fuel_candidates", []):
                    if (candidate["point"]["type"], candidate["point"]["id"]) == (
                        point["type"],
                        point["id"],
                    ):
                        exit_evidence = candidate["exit_evidence"]
                pairs.append(
                    {
                        "osm_fuel": point,
                        "native_poi": native_point,
                        "native_cache": match["cache"],
                        "separation_meters": round(separation, 1),
                        "original_exit_evidence": exit_evidence,
                        "legacy_number_unconfirmed": number_unconfirmed,
                    }
                )
        if pairs:
            proposals.append(
                {
                    "opis_id": identifier,
                    "original_names": row["names"],
                    "original_address": row["Address"],
                    "original_city": row["City"],
                    "original_state": row["State"],
                    "named_pairs": pairs,
                    "native_city_candidates": native,
                    "coordinate_verified": False,
                    "review_required": "Check unique proprietor/branch, conflicting numbers or streets, actual fuel geometry, and the original route/exit. Name similarity and a nearby native pin alone remain unverified.",
                }
            )
    proposals.sort(key=lambda row: row["opis_id"])
    (SOURCES / "supplemental-independent-name-proposals.json").write_text(
        json.dumps(proposals, indent=2) + "\n", encoding="utf-8"
    )
    native_leads = []
    for identifier, matches in sorted(native_matches.items()):
        row = rows[identifier]
        native_leads.append(
            {
                "opis_id": identifier,
                "original_names": row["names"],
                "original_address": row["Address"],
                "original_city": row["City"],
                "original_state": row["State"],
                "native_city_candidates": list(matches.values()),
                "coordinate_verified": False,
                "review_required": "A distinctive same-city name is a search lead. Verify the branch, original road or exit and actual fuel premises before using any coordinate.",
            }
        )
    (SOURCES / "supplemental-independent-name-native-leads.json").write_text(
        json.dumps(native_leads, indent=2) + "\n", encoding="utf-8"
    )
    report = {
        "unresolved_ids": len(unresolved),
        "excluded_active_ids": len(unresolved & excluded),
        "distinctive_name_target_ids": len(
            {identifier for values in indexed_rows.values() for identifier in values}
        ),
        "cached_native_records_scanned": native_count,
        "native_name_city_matches": len(native_matches),
        "targets_with_named_actual_fuel_pairs": len(proposals),
        "named_actual_fuel_pairs": sum(len(row["named_pairs"]) for row in proposals),
        "with_exact_exit_evidence": sum(
            any(p["original_exit_evidence"] for p in r["named_pairs"]) for r in proposals
        ),
        "accepted": 0,
        "network_calls": 0,
    }
    (SOURCES / "supplemental-independent-name-scan-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report))


if __name__ == "__main__":
    main()
