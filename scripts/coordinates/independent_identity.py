"""Resolve remaining independent station names against cached public directories.

This command is offline. Name similarity only selects records for review; accepted
coordinates require explicit city/state, address evidence and a physical-site check.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

from independent import extended_location_matches
from matching import normalized, station_name_keys, store_number

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "coordinate-sources"
SNAP_URL = (
    "https://services1.arcgis.com/RLQu0rK7h4kbsBq5/arcgis/rest/services/"
    "snap_retailer_location_data/FeatureServer/0/query"
)


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def street_key(value: str) -> str:
    text = value.upper()
    for pattern, replacement in (
        (r"\bINTERSTATE\b", "I"),
        (r"\b(?:HIGHWAY|HWY)\b", "HWY"),
        (r"\bUS\s+HWY\b", "US"),
        (r"\bSTATE\s+(?:ROUTE|ROAD|HWY)\b", "SR"),
        (r"\bCOUNTY\s+(?:ROAD|RD|HWY)\b", "CR"),
        (r"\bSTREET\b", "ST"),
        (r"\bAVENUE\b", "AVE"),
        (r"\bROAD\b", "RD"),
        (r"\bBOULEVARD\b", "BLVD"),
        (r"\bPARKWAY\b", "PKWY"),
        (r"\bDRIVE\b", "DR"),
        (r"\bNORTH\b", "N"),
        (r"\bSOUTH\b", "S"),
        (r"\bEAST\b", "E"),
        (r"\bWEST\b", "W"),
    ):
        text = re.sub(pattern, replacement, text)
    return normalized(text)


def name_rule(left: str, right: str, city: str) -> str | None:
    # MFA Oil identifies Petro-Card 24 as its network at
    # https://petrocard24.mfaoil.com/. Only its exact six-digit site numbers
    # bridge these operator names; shorter network numbers are not interchangeable.
    mfa_numbers = []
    for name in (left, right):
        network = re.search(r"\bMFA\b|PETRO[\s-]*(?:CARD[\s-]*24|24)", name, re.IGNORECASE)
        numbers = re.findall(r"\b9\d{5}\b", name)
        mfa_numbers.append(numbers if network else [])
    if len(mfa_numbers[0]) == len(mfa_numbers[1]) == 1:
        if mfa_numbers[0] != mfa_numbers[1]:
            return None
        return "same_published_mfa_petrocard_store_number"
    left_number, right_number = store_number(left), store_number(right)
    if left_number and right_number and left_number != right_number:
        return None
    left_keys, right_keys = station_name_keys(left, city), station_name_keys(right, city)
    if any(len(key) >= 4 for key in left_keys & right_keys):
        return "same_normalized_station_name"
    # A minor spelling error can bridge long operator names, but never a number.
    for a in left_keys:
        for b in right_keys:
            if min(len(a), len(b)) < 8 or re.findall(r"\d+", a) != re.findall(r"\d+", b):
                continue
            matcher = SequenceMatcher(None, a, b)
            changed = sum(
                max(i2 - i1, j2 - j1)
                for tag, i1, i2, j1, j2 in matcher.get_opcodes()
                if tag != "equal"
            )
            if matcher.ratio() >= 0.92 and changed <= 2:
                return "minor_station_name_spelling_variation"
    return None


def distance(left: dict, right: dict) -> float:
    a, b = math.radians(left["latitude"]), math.radians(right["latitude"])
    delta = math.radians(right["longitude"] - left["longitude"])
    return 12742000 * math.asin(
        math.sqrt(math.sin((b - a) / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(delta / 2) ** 2)
    )


def premise_rule(source: dict, point: dict, city: str, state: str) -> str | None:
    """Check a nearby premises by its recorded identity or its complete address."""
    tags = point["tags"]
    if tags.get("addr:state") and tags["addr:state"].upper() != state:
        return None
    if point["city"] and normalized(point["city"]) != normalized(city):
        return None
    point_street = (
        f"{tags['addr:housenumber']} {tags['addr:street']}"
        if tags.get("addr:housenumber") and tags.get("addr:street")
        else ""
    )
    if point_street and street_key(point_street) != street_key(source["address"]):
        return None
    names = [
        tags[key]
        for key in ("name", "brand", "operator", "alt_name", "official_name")
        if tags.get(key)
    ]
    if tags.get("ref") and re.fullmatch(r"\d+", tags["ref"]):
        names.extend(f"{name} #{tags['ref']}" for name in names.copy())
    if any(name_rule(source["name"], name, city) for name in names):
        return "same_recorded_premises_name_or_number"
    # Fuel brands can differ from the shop/operator name. A complete matching
    # street address locates the same premises without asserting a rebrand.
    if (
        tags.get("addr:housenumber")
        and tags.get("addr:street")
        and (
            tags.get("amenity") == "fuel"
            or tags.get("shop") == "convenience"
            or tags.get("highway") == "services"
        )
        and street_key(f"{tags['addr:housenumber']} {tags['addr:street']}")
        == street_key(source["address"])
    ):
        return "same_complete_street_at_mapped_station_premises"
    return None


def catalog() -> tuple[list[dict], list[dict]]:
    records = {}
    for network in ("fuelman", "cfn"):
        path = DATA / f"{network}-stations.json"
        if not path.exists():
            continue
        for row in read(path):
            # CFN and Fuelman share Corpay record IDs and coordinates.
            key = ("corpay", str(row["id"]))
            if key in records:
                continue
            highway = re.sub(r"^H(?=\d)", "HWY ", row.get("highway") or "")
            highway += f" EXIT {row['exitNumber']}" if row.get("exitNumber") else ""
            records[key] = {
                "name": row["name"],
                "address": row["address1"],
                "city": row["city"],
                "state": row["stateCode"],
                "latitude": row["lat"],
                "longitude": row["lng"],
                "highway_address": highway,
                "source": f"{network}_public_station_locator",
                "external_id": str(row["id"]),
                "source_url": row["source_url"],
            }
    for filename in ("independent-ambest-stations.json", "catscale-stations.json"):
        for row in read(DATA / filename):
            source = (
                "ambest_official_location" if "ambest" in filename else "catscale_official_location"
            )
            records[(source, row["external_id"])] = {**row, "source": source}
    snap = {}
    for path in sorted((DATA / "snap-current-pages").glob("*.json")):
        for feature in read(path).get("features", []):
            row = feature["attributes"]
            identifier = str(row["Record_ID"])
            snap[identifier] = {
                "name": row["Store_Name"],
                "address": row["Store_Street_Address"] or "",
                "city": row["City"] or "",
                "state": row["State"],
                "latitude": row["Latitude"],
                "longitude": row["Longitude"],
                "source": "official_usda_snap_retailer_coordinate",
                "external_id": f"usda:{identifier}",
                "source_url": f"{SNAP_URL}?f=json&where=Record_ID%3D{identifier}&outFields=*&returnGeometry=true",
            }
    records.update({("snap", key): row for key, row in snap.items()})
    premises = []
    for path in sorted((DATA / "osm-states").glob("*.json")):
        payload = read(path)
        for row in payload.get("records", []):
            tags = row["tags"]
            if (
                tags.get("name")
                or tags.get("brand")
                or (tags.get("addr:housenumber") and tags.get("addr:street"))
            ):
                premises.append(
                    {
                        **row,
                        "name": tags.get("name") or tags.get("brand") or "unnamed station premises",
                        "city": tags.get("addr:city", ""),
                        "source_url": f"https://www.openstreetmap.org/{row['type']}/{row['id']}",
                    }
                )
    return list(records.values()), premises


def resolve(
    pending: list[dict], records: list[dict], premises: list[dict]
) -> tuple[list[dict], list[dict]]:
    by_city, comdata, nearby = defaultdict(list), defaultdict(list), defaultdict(list)
    for record in records:
        if not all(
            isinstance(record.get(key), (int, float)) and math.isfinite(record[key])
            for key in ("latitude", "longitude")
        ):
            continue
        if not (18 <= record["latitude"] <= 72 and -180 <= record["longitude"] <= -65):
            continue
        by_city[(record["state"], normalized(record["city"]))].append(record)
    for row in read(DATA / "supplemental-comdata-addresses.json"):
        comdata[(row["state"], normalized(row["city"]))].append(row)
    for row in premises:
        nearby[(math.floor(row["latitude"] * 100), math.floor(row["longitude"] * 100))].append(row)
    accepted, unresolved = [], []
    for station in pending:
        city_key = (station["state"], normalized(station["city"]))
        candidates, considered = [], []
        for source in by_city[city_key]:
            rule = next(
                (
                    rule
                    for name in station["names"]
                    if (rule := name_rule(name, source["name"], station["city"]))
                ),
                None,
            )
            if not rule:
                continue
            evidence = (
                ["same_published_operator_site_number"]
                if rule == "same_published_mfa_petrocard_store_number"
                else []
            )
            for address in (source["address"], source.get("highway_address", "")):
                if address:
                    evidence.extend(
                        extended_location_matches(
                            {"Address": station["address"]}, {"address": address}
                        )
                    )
            address_records = [
                row
                for row in comdata[city_key]
                if name_rule(station["names"][0], row["name"], station["city"])
                and street_key(row["address"]) == street_key(source["address"])
            ]
            if address_records:
                evidence.append("same_actual_street_in_comdata_directory")
            considered.append({"source": source, "name_rule": rule, "address_evidence": evidence})
            if not evidence:
                continue
            lat_key, lon_key = (
                math.floor(source["latitude"] * 100),
                math.floor(source["longitude"] * 100),
            )
            physical = []
            for lat in range(lat_key - 1, lat_key + 2):
                for lon in range(lon_key - 1, lon_key + 2):
                    for point in nearby[(lat, lon)]:
                        if distance(source, point) <= 200 and (
                            point_rule := premise_rule(
                                source, point, station["city"], station["state"]
                            )
                        ):
                            physical.append({**point, "match_rule": point_rule})
            snap_support = [
                other
                for other in by_city[city_key]
                if other["source"] == "official_usda_snap_retailer_coordinate"
                and other["source"] != source["source"]
                and street_key(other["address"]) == street_key(source["address"])
                and name_rule(source["name"], other["name"], station["city"])
                and distance(source, other) <= 100
            ]
            if not physical:
                continue
            citations = [
                {
                    "source_url": source["source_url"],
                    "reason": f"Explicit same city/state; {rule}; {', '.join(sorted(set(evidence)))}.",
                }
            ]
            if rule == "same_published_mfa_petrocard_store_number":
                citations.append(
                    {
                        "source_url": "https://petrocard24.mfaoil.com/",
                        "reason": "MFA Oil identifies Petro-Card 24 as its network. The same six-digit site number and explicit city/state establish this operator-name bridge; shorter network numbers are not treated as aliases.",
                    }
                )
            citations.extend(
                {
                    "source_url": row["source_url"],
                    "reason": f"Comdata independently identifies {row['name']} at {row['address']}, {row['city']}, {row['state']}.",
                }
                for row in address_records
            )
            citations.extend(
                {
                    "source_url": row["source_url"],
                    "reason": f"Mapped physical premises {row['name']} ({row['latitude']:.7f}, {row['longitude']:.7f}); {row['match_rule']}; agree within {round(distance(source, row))} meters.",
                }
                for row in physical
            )
            citations.extend(
                {
                    "source_url": row["source_url"],
                    "reason": f"Current SNAP record independently identifies the same name/street and agrees within {round(distance(source, row))} meters; street interpolation alone is not treated as physical-premises proof.",
                }
                for row in snap_support
            )
            point = min(
                physical,
                key=lambda row: (
                    row["tags"].get("amenity") != "fuel",
                    row["match_rule"] != "same_complete_street_at_mapped_station_premises",
                    distance(source, row),
                    row["type"],
                    row["id"],
                ),
            )
            citations.append(
                {
                    "source_url": point["source_url"],
                    "reason": "Returned coordinates are the verified physical premises point, rather than the directory's nearby street or business centroid.",
                }
            )
            candidates.append(
                {
                    "opis_id": station["opis_id"],
                    "latitude": point["latitude"],
                    "longitude": point["longitude"],
                    "source": "independent_identity_crosschecked_public_directories",
                    "source_url": source["source_url"],
                    "external_id": source["external_id"],
                    "matched_name": source["name"],
                    "matched_address": f"{source['address']}, {source['city']}, {source['state']}",
                    "evidence": citations,
                    "availability": source.get("availability", "unknown"),
                    "original_names": station["names"],
                    "original_address": station["address"],
                    "original_city": station["city"],
                    "original_state": station["state"],
                }
            )
        if candidates and all(distance(candidates[0], other) <= 300 for other in candidates[1:]):
            # Prefer the point directly corroborated by mapped premises; conflicting
            # city-local sites remain unresolved rather than selected by similarity.
            chosen = max(
                candidates,
                key=lambda row: sum(
                    "openstreetmap.org" in item["source_url"] for item in row["evidence"]
                ),
            )
            accepted.append(chosen)
        elif considered:
            unresolved.append(
                {
                    **station,
                    "reason": "conflicting_physical_sites"
                    if candidates
                    else "additional_identity_or_premises_evidence_needed",
                    "candidates": considered,
                }
            )
    used = defaultdict(list)
    for row in accepted:
        used[(row["source_url"], row["external_id"])].append(row["opis_id"])
    collisions = {opis for values in used.values() if len(values) > 1 for opis in values}
    unresolved.extend(
        {"opis_id": opis, "reason": "same_source_matched_multiple_opis_ids"}
        for opis in sorted(collisions)
    )
    return [row for row in accepted if row["opis_id"] not in collisions], unresolved


def targets(refresh: bool = False) -> list[dict]:
    """Preserve inputs after the shared merger removes accepted unresolved IDs."""
    path = DATA / "independent-identity-targets.json"
    if path.exists() and not refresh:
        return read(path)
    rows = {row["opis_id"]: row for row in read(path)} if path.exists() else {}
    rows.update({row["opis_id"]: row for row in read(DATA / "unresolved.json")})
    previous = DATA / "independent-identity-candidates.json"
    if previous.exists():
        for row in read(previous):
            if row.get("original_names"):
                rows.setdefault(
                    row["opis_id"],
                    {
                        "opis_id": row["opis_id"],
                        "names": row["original_names"],
                        "address": row["original_address"],
                        "city": row["original_city"],
                        "state": row["original_state"],
                    },
                )
    result = [rows[key] for key in sorted(rows)]
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-targets", action="store_true")
    args = parser.parse_args()
    records, premises = catalog()
    accepted, unresolved = resolve(targets(args.refresh_targets), records, premises)
    for label, rows in (("candidates", accepted), ("unresolved", unresolved)):
        (DATA / f"independent-identity-{label}.json").write_text(
            json.dumps(rows, indent=2) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "source_records": len(records),
                "physical_records": len(premises),
                "candidates": len(accepted),
                "needs_review": len(unresolved),
                "network_calls": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
