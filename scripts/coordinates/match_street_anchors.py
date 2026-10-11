"""Qualify named mapped premises using recovered, exact numeric street anchors."""

import argparse
import json
import math
from collections import Counter

from shapely import STRtree
from shapely.geometry import Point, box

from scripts.coordinates.match_osm import (
    GEOD,
    SOURCES,
    distinct_sites,
    identity,
    load_pois,
    stations,
)
from scripts.coordinates.matching import normalized
from scripts.coordinates.regional import normalized_address


def main(directories=False, network_audit=False):
    prefix = (
        "network-audit-census"
        if network_audit
        else "directory-street-census"
        if directories
        else "street-census"
    )
    output_prefix = (
        "network-audit-street-anchors"
        if network_audit
        else "directory-street-anchors"
        if directories
        else "street-anchors"
    )
    rows = stations()
    identities = {
        item["opis_id"]: item
        for item in json.loads((SOURCES / f"{prefix}-identity-evidence.json").read_text())
    }
    by_state = load_pois()
    indexes = {
        state: STRtree([Point(point["longitude"], point["latitude"]) for point in points])
        for state, points in by_state.items()
    }
    accepted, proposals, ambiguous = [], [], []
    for anchor in json.loads((SOURCES / f"{prefix}-anchors.json").read_text()):
        if anchor["status"] != "Match" or anchor["match_type"] != "Exact":
            continue
        identifier = anchor["opis_id"]
        row, published = rows[identifier], identities[identifier]
        if normalized_address(anchor["matched_address"].split(",")[0]) != normalized_address(
            published["matched_address"]
        ):
            continue
        state = row["State"]
        longitude, latitude = anchor["longitude"], anchor["latitude"]
        width = 0.003 / math.cos(math.radians(latitude))
        bounds = box(longitude - width, latitude - 0.003, longitude + width, latitude + 0.003)
        matches, nearby = [], []
        for index in indexes[state].query(bounds):
            point = by_state[state][index]
            distance = abs(GEOD.inv(longitude, latitude, point["longitude"], point["latitude"])[2])
            if distance > 250:
                continue
            tags = point["tags"]
            nearby.append({**point, "distance_from_street_anchor_meters": round(distance, 1)})
            rule = identity(row, point)
            if not rule:
                continue
            if tags.get("addr:city") and normalized(tags["addr:city"]) != normalized(row["City"]):
                continue
            mapped_street = " ".join(
                tags.get(key, "") for key in ("addr:housenumber", "addr:street")
            ).strip()
            if tags.get("addr:housenumber") and normalized_address(
                mapped_street
            ) != normalized_address(published["matched_address"]):
                continue
            matches.append(
                {**point, "identity_rule": rule, "anchor_distance_meters": round(distance, 1)}
            )
        sites = distinct_sites(matches)
        if len(sites) != 1:
            if sites:
                ambiguous.append({"opis_id": identifier, "candidates": sites})
            if nearby:
                proposals.append(
                    {
                        "opis_id": identifier,
                        "published_identity": published,
                        "anchor": anchor,
                        "nearby_premises": nearby,
                        "review_status": "unverified_manual_review_required",
                    }
                )
            continue
        point = sites[0]
        accepted.append(
            {
                "opis_id": identifier,
                "latitude": point["latitude"],
                "longitude": point["longitude"],
                "source": "mapped_named_premises_with_published_street_identity",
                "source_url": f"https://www.openstreetmap.org/{point['type']}/{point['id']}",
                "external_id": f"{point['type']}/{point['id']}",
                "matched_name": point["tags"].get("name", ""),
                "matched_address": published["matched_address"],
                "evidence": [
                    point["identity_rule"],
                    {
                        "source_url": published["source_url"],
                        "external_id": published["external_id"],
                        "reason": "The same published retailer identity binds this numeric street address to the CSV station.",
                    },
                    {
                        "source_url": "https://geocoding.geo.census.gov/geocoder/locations/addressbatch",
                        "reason": "An exact normalized house/street match bounds the search; the accepted point is the independently mapped named premises, not the interpolated Census coordinate.",
                        "anchor_distance_meters": point["anchor_distance_meters"],
                    },
                ],
            }
        )
    for name, records in (
        (f"{output_prefix}-candidates", accepted),
        (f"{output_prefix}-ambiguous", ambiguous),
        (f"{output_prefix}-review-proposals", proposals),
    ):
        (SOURCES / f"{name}.json").write_text(
            json.dumps(records, indent=2) + "\n", encoding="utf-8"
        )
    print(
        {
            "named_premises": len(accepted),
            "manual_proposals": len(proposals),
            "ambiguous": len(ambiguous),
            "rules": dict(Counter(item["evidence"][0] for item in accepted)),
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--directories", action="store_true")
    mode.add_argument("--network-audit", action="store_true")
    args = parser.parse_args()
    main(args.directories, args.network_audit)
