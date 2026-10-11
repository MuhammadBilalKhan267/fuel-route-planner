"""Cross-check transient search results against independently sourced stations.

Only original inputs and existing public/open source records can enter returned
candidates. Provider place records are used in memory and never copied into them.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections import defaultdict
from pathlib import Path

from shapely.geometry import Point

from scripts.coordinates.matching import brand, normalized, station_name, store_number
from scripts.coordinates.regional import normalized_address
from scripts.coordinates.tomtom import assess_response

GENERIC_NAMES = {
    "shell",
    "bp",
    "chevron",
    "exxon",
    "mobil",
    "texaco",
    "marathon",
    "sunoco",
    "valero",
    "citgo",
    "amoco",
    "arco",
    "conoco",
    "phillips66",
    "76",
    "union76",
    "sinclair",
    "cenex",
    "gulf",
    "irving",
}


def _read(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _numeric_street(value):
    return isinstance(value, str) and bool(re.match(r"^\s*\d[\dA-Z-]*\s+", value, re.IGNORECASE))


def _same_identity(original, proposed):
    expected = {store_number(name) for name in original["names"]} - {None}
    actual = store_number(proposed)
    if len(expected) > 1 or (expected and actual not in expected):
        return False
    if actual and not expected:
        return False
    return station_name(proposed) in {station_name(name) for name in original["names"]}


def _distance(left, right):
    a, b = math.radians(left["latitude"]), math.radians(right["latitude"])
    delta = math.radians(right["longitude"] - left["longitude"])
    sine = math.sin((b - a) / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(delta / 2) ** 2
    return 12_742_000 * math.asin(math.sqrt(min(1, max(0, sine))))


def _valid_point(row):
    return all(
        isinstance(row.get(key), (int, float))
        and not isinstance(row[key], bool)
        and math.isfinite(row[key])
        and lower <= row[key] <= upper
        for key, lower, upper in (("latitude", 18, 72), ("longitude", -180, -65))
    )


def _compatible_fuel_name(source, point):
    tags = point["tags"]
    source_brand = brand(source["name"])
    mapped_brands = {brand(tags.get(key, "")) for key in ("brand", "name")} - {None}
    if len(mapped_brands) > 1 or (
        source_brand and any(mapped_brand != source_brand for mapped_brand in mapped_brands)
    ):
        return False
    name = tags.get("name", "")
    if not name or station_name(name) in GENERIC_NAMES:
        return True
    expected_number, actual_number = store_number(source["name"]), store_number(name)
    if actual_number and actual_number != expected_number:
        return False
    expected_name = source["name"]
    if expected_number:
        expected_name = re.sub(rf"(?<!\d)#?\s*0*{re.escape(expected_number)}\b", "", expected_name)
    return station_name(name) == station_name(expected_name)


def _independent_leads(data_dir):
    leads = defaultdict(list)
    for row in _read(data_dir / "independent-identity-proposals.json"):
        leads[row["opis_id"]].extend(
            {**item["source"], "cached_source": "independent-identity-proposals.json"}
            for item in row.get("possible", [])
        )
    for row in _read(data_dir / "independent-identity-unresolved.json"):
        leads[row["opis_id"]].extend(
            {**item["source"], "cached_source": "independent-identity-unresolved.json"}
            for item in row.get("candidates", [])
        )
    for row in _read(data_dir / "epa-ust-pending-identity-proposals.json"):
        leads[row["opis_id"]].append(
            {
                **row,
                "address": row.get("street", ""),
                "cached_source": "epa-ust-pending-identity-proposals.json",
            }
        )
    for row in _read(data_dir / "supplemental-independent-extended-native-leads.json"):
        for item in row.get("native_city_candidates", []):
            point = item["point"]
            if point.get("category") != "gas_station" or point.get("operating_status") == "closed":
                continue
            for address in point.get("addresses", []):
                if address.get("country") == "US":
                    leads[row["opis_id"]].append(
                        {
                            **point,
                            "address": address.get("freeform", ""),
                            "city": address.get("locality", ""),
                            "state": address.get("region", ""),
                            "cached_source": item.get(
                                "cache", "supplemental-independent-extended-native-leads.json"
                            ),
                        }
                    )
    for row in _read(data_dir / "regional-exit-brand-route-proposals.json"):
        for choice in row.get("primary_branch_choices", []):
            feature = choice["primary_branch"]
            properties = feature["properties"]
            point = feature.get("geometry", {}).get("coordinates", [])
            if len(point) != 2:
                continue
            leads[row["opis_id"]].append(
                {
                    "name": properties.get("name", ""),
                    "address": properties.get("addr:street_address", ""),
                    "city": properties.get("addr:city", ""),
                    "state": properties.get("addr:state", ""),
                    "latitude": point[1],
                    "longitude": point[0],
                    "external_id": properties.get("ref", feature.get("id", "")),
                    "source_url": properties.get("@source_uri", choice.get("dataset_url", "")),
                    "cached_source": choice.get(
                        "cached_source", "regional-exit-brand-route-proposals.json"
                    ),
                }
            )
    # An exit and a nearby fuel feature do not establish a branch or street.
    # regional-exit-single-fuel-proposals.json therefore supplies no known address.
    return leads


def build_contexts(data_dir: Path, pending_ids=None, *, original_rows=None):
    """Rebuild source evidence for original rows, including previously merged IDs."""
    data_dir = Path(data_dir)
    pending = _read(data_dir / "unresolved.json")
    pending_by_id = {row["opis_id"]: row for row in pending}
    originals = pending if original_rows is None else original_rows
    held = set()
    for path in [
        *data_dir.glob("*-held-identities.json"),
        data_dir / "unresolved-identity-reviews.json",
    ]:
        held.update(row["opis_id"] for row in _read(path) if "opis_id" in row)
    leads = _independent_leads(data_dir)
    bound_urls = defaultdict(set)
    for accepted in _read(data_dir / "merged.json"):
        urls = [accepted.get("source_url", "")]
        urls.extend(
            item.get("source_url", "")
            for item in accepted.get("evidence", [])
            if isinstance(item, dict)
        )
        for url in urls:
            if url.startswith("https://www.openstreetmap.org/"):
                bound_urls[url].add(accepted["opis_id"])
    fuel_grid = defaultdict(list)
    for path in sorted((data_dir / "osm-states").glob("*.json")):
        for point in _read(path).get("records", []):
            if point.get("tags", {}).get("amenity") == "fuel" and _valid_point(point):
                fuel_grid[
                    (math.floor(point["latitude"] * 100), math.floor(point["longitude"] * 100))
                ].append({**point, "cached_source": f"osm-states/{path.name}"})
    contexts = {}
    for original in originals:
        identifier = original["opis_id"]
        if pending_ids is not None and identifier not in pending_ids:
            continue
        row = copy.deepcopy(original)
        for key in ("identity_review", "identity_collisions"):
            if key in pending_by_id.get(identifier, {}):
                row[key] = copy.deepcopy(pending_by_id[identifier][key])
        context = {"row": row, "candidate": None, "reason": "no_independent_branch"}
        contexts[identifier] = context
        if identifier in held or row.get("identity_review") or row.get("identity_collisions"):
            context["reason"] = "held_identity"
            row["identity_review"] = "Previous independent branch conflict remains unresolved."
            continue
        sources = [
            source
            for source in leads[identifier]
            if source.get("state", "").strip().upper() == row["state"]
            and normalized(source.get("city", "")) == normalized(row["city"])
            and _same_identity(row, source.get("name", ""))
            and _numeric_street(source.get("address"))
            and source.get("source_url")
            and _valid_point(source)
        ]
        streets = {normalized_address(source["address"]) for source in sources}
        if len(streets) != 1:
            context["reason"] = "ambiguous_independent_streets" if streets else context["reason"]
            continue
        # A generic brand cannot be tied to an original exit from name/city alone.
        distinctive = any(
            station_name(name)
            and station_name(name) not in GENERIC_NAMES
            and station_name(name) != brand(name)
            and station_name(name) != station_name(row["city"])
            for name in row["names"]
        )
        numbered = any(store_number(name) for name in row["names"])
        original_street = _numeric_street(row["address"])
        if original_street and normalized_address(row["address"]) not in streets:
            context["reason"] = "original_street_conflict"
            continue
        if not distinctive and not numbered and not original_street:
            context["reason"] = (
                "city_only_name_without_original_street"
                if all(station_name(name) == station_name(row["city"]) for name in row["names"])
                else "generic_brand_without_original_street"
            )
            continue
        source = sources[0]
        if any(_distance(source, other) > 100 for other in sources[1:]):
            context["reason"] = "independent_coordinate_conflict"
            continue
        row["known_address"] = source["address"]
        nearby = {}
        lat, lon = math.floor(source["latitude"] * 100), math.floor(source["longitude"] * 100)
        for a in range(lat - 1, lat + 2):
            for b in range(lon - 1, lon + 2):
                for point in fuel_grid[(a, b)]:
                    if _distance(source, point) > 100:
                        continue
                    tags = point["tags"]
                    street = (
                        f"{tags.get('addr:housenumber', '')} {tags.get('addr:street', '')}".strip()
                    )
                    if tags.get("addr:state") and tags["addr:state"].upper() != row["state"]:
                        continue
                    if tags.get("addr:city") and normalized(tags["addr:city"]) != normalized(
                        row["city"]
                    ):
                        continue
                    if _numeric_street(street) and normalized_address(street) not in streets:
                        continue
                    if not _compatible_fuel_name(source, point):
                        continue
                    nearby[(point["type"], point["id"])] = point
        if not nearby:
            context["reason"] = "no_actual_mapped_fuel_within_100m"
            continue
        points = list(nearby.values())
        if len(points) != 1:
            context["reason"] = "multiple_mapped_fuel_properties"
            continue
        point = min(
            points, key=lambda item: (_distance(source, item), item["type"], str(item["id"]))
        )
        point_url = f"https://www.openstreetmap.org/{point['type']}/{point['id']}"
        if bound_urls[point_url] - {identifier}:
            context["reason"] = "mapped_fuel_bound_to_another_station"
            continue
        context["candidate"] = {
            "opis_id": identifier,
            "latitude": point["latitude"],
            "longitude": point["longitude"],
            "source": "mapped_fuel_with_published_street_identity",
            "source_url": point_url,
            "external_id": f"osm:{point['type']}:{point['id']}",
            "matched_name": source["name"],
            "matched_address": f"{source['address']}, {source['city']}, {source['state']}",
            "original_names": row["names"],
            "original_address": row["address"],
            "original_city": row["city"],
            "original_state": row["state"],
            "availability": "unknown",
            "evidence": [
                {
                    "source_url": item["source_url"],
                    "cached_source": item["cached_source"],
                    "source_record": copy.deepcopy(item),
                    "reason": "Same exact normalized station identity, city/state and single numeric street in independently cached public source.",
                }
                for item in sources
            ]
            + [
                {
                    "source_url": point_url,
                    "cached_source": point["cached_source"],
                    "source_record": copy.deepcopy(point),
                    "reason": "Coordinates come entirely from this mapped amenity=fuel feature within 100 meters of the independent station record; conflicting complete mapped addresses and branch names were excluded.",
                }
            ],
            "notes": "Automated independent identity and fuel-premises cross-check. No search-provider place content or coordinates retained; operating status remains unknown.",
        }
        context["reason"] = "independent_branch_and_mapped_fuel"
    return contexts


def crosscheck_independent(context, payload, state_boundaries):
    """Return an independent candidate only after one transient matching result."""
    candidate = context.get("candidate")
    if candidate is None:
        return None
    boundary = state_boundaries.get(context["row"]["state"])
    if boundary is None or not boundary.covers(
        Point(candidate["longitude"], candidate["latitude"])
    ):
        return None
    assessment = assess_response(context["row"], payload, state_boundaries)
    if assessment["status"] != "unique_branch_proposal":
        return None
    proposal = assessment["proposals"][0]
    if _distance(candidate, proposal) > 100:
        return None
    return copy.deepcopy(candidate)
