"""Strict current fuel-branch identities for review; locator pins stay unverified."""

import json
import math
import re
from collections import defaultdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from scripts.coordinates.exit_anchors import csv_exit_request
from scripts.coordinates.match_osm import GEOD, ROOT, SOURCES
from scripts.coordinates.matching import brand, normalized, store_number
from scripts.coordinates.regional import normalized_address

TOKEN_FIELDS = {
    "key",
    "api_key",
    "apikey",
    "clientid",
    "client_id",
    "token",
    "access_token",
    "appid",
}


def public_url(value):
    parts = urlsplit(value)
    query = [(key, val) for key, val in parse_qsl(parts.query) if key.lower() not in TOKEN_FIELDS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def strip_tokens(value):
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return public_url(value)
    if isinstance(value, dict):
        return {key: strip_tokens(val) for key, val in value.items()}
    if isinstance(value, list):
        return [strip_tokens(val) for val in value]
    return value


def fuel_features(value):
    if not isinstance(value, dict) or not isinstance(value.get("features"), list):
        raise TypeError("Cached source is not a feature collection")
    for feature in value["features"]:
        properties = feature.get("properties", {})
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates", [])
        if (
            properties.get("addr:country") == "US"
            and properties.get("amenity") == "fuel"
            and geometry.get("type") == "Point"
            and len(coordinates) >= 2
            and all(
                isinstance(item, (int, float)) and math.isfinite(item) for item in coordinates[:2]
            )
            and -180 <= coordinates[0] <= 180
            and -90 <= coordinates[1] <= 90
        ):
            yield strip_tokens(feature)


def branch_identity(row, feature):
    properties = feature["properties"]
    actual_brand = brand(properties.get("brand", ""))
    if actual_brand not in {brand(name) for name in row["names"]} - {None}:
        return None
    if properties.get("addr:state") != row["state"] or normalized(
        properties.get("addr:city", "")
    ) != normalized(row["city"]):
        return None
    expected = {store_number(name) for name in row["names"]} - {None}
    ref = properties.get("ref", "")
    actual = str(int(ref)) if ref.isdecimal() else None
    if expected:
        return "same_current_primary_brand_store_number_city_state" if actual in expected else None
    address = row["address"]
    if re.match(r"^\s*\d[\dA-Z-]*\s+", address, re.IGNORECASE) and normalized_address(
        address
    ) == normalized_address(properties.get("addr:street_address", "")):
        return "same_current_primary_brand_numeric_street_city_state"
    return None


def _feed_index(feeds):
    index = defaultdict(list)
    for path, value in feeds:
        brand_cache = {}
        for feature in fuel_features(value):
            properties = feature["properties"]
            name = properties.get("brand", "")
            if name not in brand_cache:
                brand_cache[name] = brand(name)
            key = (
                brand_cache[name],
                normalized(properties.get("addr:city", "")),
                properties.get("addr:state"),
            )
            index[key].append(
                (
                    path,
                    feature,
                    public_url(
                        value.get("source_url", value.get("acquisition", {}).get("source_url", ""))
                    ),
                )
            )
    return index


def prepare(rows, feeds, mapped_points):
    index = _feed_index(feeds)
    proposals = []
    for row in rows:
        for expected_brand in {brand(name) for name in row["names"]} - {None}:
            for path, feature, dataset_url in index[
                (expected_brand, normalized(row["city"]), row["state"])
            ]:
                rule = branch_identity(row, feature)
                if not rule:
                    continue
                longitude, latitude = feature["geometry"]["coordinates"][:2]
                street = feature["properties"].get("addr:street_address", "")
                nearby = []
                for point in mapped_points.get(row["state"], []):
                    distance = abs(
                        GEOD.inv(longitude, latitude, point["longitude"], point["latitude"])[2]
                    )
                    if distance > 800:
                        continue
                    tags = point["tags"]
                    actual_street = " ".join(
                        tags.get(key, "") for key in ("addr:housenumber", "addr:street")
                    ).strip()
                    nearby.append(
                        {
                            "point": strip_tokens(point),
                            "distance_from_primary_marker_m": round(distance, 1),
                            "same_published_street": bool(actual_street)
                            and normalized_address(actual_street) == normalized_address(street),
                            "source_url": f"https://www.openstreetmap.org/{point['type']}/{point['id']}",
                            "coordinate_verified": False,
                        }
                    )
                proposals.append(
                    {
                        "opis_id": row["opis_id"],
                        "original_names": row["names"],
                        "original_address": row["address"],
                        "original_city": row["city"],
                        "original_state": row["state"],
                        "identity_rule": rule,
                        "primary_branch": feature,
                        "dataset_url": dataset_url,
                        "cached_source": str(path.relative_to(SOURCES)),
                        "nearby_mapped_fuel_leads": sorted(
                            nearby, key=lambda point: point["distance_from_primary_marker_m"]
                        ),
                        "coordinate_verified": False,
                        "review_status": "requires_actual_fuel_equipment_and_original_road_review",
                    }
                )
    return proposals


def route_branch_proposals(rows, exit_proposals, feeds, excluded_ids=()):
    """Preserve every nearby branch choice; exit proximity establishes no identity."""
    pending = {row["opis_id"]: row for row in rows}
    excluded = set(excluded_ids)
    index = _feed_index(feeds)
    results = []
    for exit_proposal in exit_proposals:
        row = pending.get(exit_proposal["opis_id"])
        if not row or row["opis_id"] in excluded:
            continue
        if any(store_number(name) is not None for name in row["names"]):
            continue
        request = csv_exit_request(row["address"])
        if not request or request != exit_proposal["request"]:
            continue
        expected_brands = {brand(name) for name in row["names"]} & {
            "shell",
            "bp",
            "chevron",
            "texaco",
        }
        choices = []
        anchors = [
            anchor
            for anchor in exit_proposal["anchors"]
            if anchor["state"] == row["state"]
            and anchor["route"] in request["routes"]
            and anchor["exit_ref"] == request["exit_ref"]
        ]
        if not anchors:
            continue
        for expected_brand in expected_brands:
            for path, feature, dataset_url in index[
                (expected_brand, normalized(row["city"]), row["state"])
            ]:
                longitude, latitude = feature["geometry"]["coordinates"][:2]
                distance = min(
                    abs(GEOD.inv(longitude, latitude, a["longitude"], a["latitude"])[2])
                    for a in anchors
                )
                if distance > 2000:
                    continue
                street = feature["properties"].get("addr:street_address", "")
                mapped_leads = []
                for item in exit_proposal["fuel_candidates"]:
                    point = item["point"]
                    tags = point["tags"]
                    if brand(tags.get("brand", tags.get("name", ""))) != expected_brand:
                        continue
                    separation = abs(
                        GEOD.inv(longitude, latitude, point["longitude"], point["latitude"])[2]
                    )
                    if separation > 100:
                        continue
                    mapped_street = " ".join(
                        tags.get(key, "") for key in ("addr:housenumber", "addr:street")
                    ).strip()
                    mapped_leads.append(
                        {
                            "point": strip_tokens(point),
                            "source_url": f"https://www.openstreetmap.org/{point['type']}/{point['id']}",
                            "distance_from_primary_marker_m": round(separation, 1),
                            "same_published_street": bool(mapped_street)
                            and normalized_address(mapped_street) == normalized_address(street),
                            "coordinate_verified": False,
                        }
                    )
                choices.append(
                    {
                        "primary_branch": feature,
                        "dataset_url": dataset_url,
                        "cached_source": str(path.relative_to(SOURCES)),
                        "distance_from_exit_anchor_m": round(distance, 1),
                        "mapped_fuel_leads": mapped_leads,
                        "identity_verified": False,
                        "coordinate_verified": False,
                    }
                )
        if choices:
            results.append(
                {
                    "opis_id": row["opis_id"],
                    "original_names": row["names"],
                    "original_address": row["address"],
                    "original_city": row["city"],
                    "original_state": row["state"],
                    "request": exit_proposal["request"],
                    "anchors": anchors,
                    "primary_branch_choices": choices,
                    "identity_verified": False,
                    "coordinate_verified": False,
                    "review_status": "requires_original_exit_branch_and_actual_equipment_review",
                }
            )
    return results


def main():
    rows = json.loads((SOURCES / "unresolved.json").read_text(encoding="utf-8"))
    paths = [
        SOURCES / "fuel-locators/shell.json",
        SOURCES / "fuel-locators/chevron_us.json",
        SOURCES / "minor-chains/bp.json",
    ]
    feeds = [
        (path, json.loads(path.read_text(encoding="utf-8"))) for path in paths if path.exists()
    ]
    mapped_points = defaultdict(list)
    states = json.loads((ROOT / "trips/data/us-states.geojson").read_text(encoding="utf-8"))
    abbreviations = {
        normalized(feature["properties"]["NAME"]): feature["properties"]["STUSAB"]
        for feature in states["features"]
    }
    for path in (SOURCES / "osm-states").glob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        state = abbreviations.get(normalized(path.stem))
        if state is None:
            continue
        mapped_points[state].extend(
            point for point in value["records"] if point["tags"].get("amenity") == "fuel"
        )
    proposals = prepare(rows, feeds, mapped_points)
    (SOURCES / "regional-exit-brand-identity-proposals.json").write_text(
        json.dumps(proposals, indent=2) + "\n", encoding="utf-8"
    )
    report = {
        "reviewed_at": "2026-10-11",
        "source_files": [str(path.relative_to(SOURCES)) for path in paths],
        "proposal_rows": len(proposals),
        "unique_opis_ids": len({row["opis_id"] for row in proposals}),
        "coordinate_verified": False,
        "http_calls": 0,
    }
    (SOURCES / "regional-exit-brand-identity-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    exclusions = set()
    for name in (
        "regional-exit-single-fuel-proposals.json",
        "regional-exit-nastc-identity-proposals.json",
        "regional-exit-conflict-reviews.json",
        "regional-exit-brand-conflict-reviews.json",
    ):
        path = SOURCES / name
        if path.exists():
            exclusions.update(
                row["opis_id"] for row in json.loads(path.read_text(encoding="utf-8"))
            )
    route_proposals = route_branch_proposals(
        rows,
        json.loads((SOURCES / "regional-exit-proposals.json").read_text(encoding="utf-8")),
        feeds,
        exclusions,
    )
    (SOURCES / "regional-exit-brand-route-proposals.json").write_text(
        json.dumps(route_proposals, indent=2) + "\n", encoding="utf-8"
    )
    print(report)
    print({"unverified_route_proposals": len(route_proposals)})


if __name__ == "__main__":
    main()
