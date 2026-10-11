"""Reproduce explicit station-premises reviews from cached identity evidence."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys

from loves_ta import CSV, DATA, write_json
from loves_ta_conflicts import independent_records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--distinct-conflicts", action="store_true")
    group.add_argument("--identity-bridges", action="store_true")
    group.add_argument("--network-audit", action="store_true")
    args = parser.parse_args()
    prefix = (
        "supplemental-distinct"
        if args.distinct_conflicts
        else "supplemental-identitybridges"
        if args.identity_bridges
        else "supplemental-network-audit"
        if args.network_audit
        else "supplemental-newchains"
    )
    identities = json.loads((DATA / f"{prefix}-identities.json").read_text(encoding="utf-8"))
    records = {(r["type"], r["id"]): r for r in independent_records()}
    with CSV.open(encoding="utf-8-sig", newline="") as handle:
        original = {int(r["OPIS Truckstop ID"]): r for r in csv.DictReader(handle)}
    raw_candidates = json.loads(
        (DATA / f"{prefix}-raw-candidates.json").read_text(encoding="utf-8")
    )
    accepted, rejected, pending = [], [], []
    for identity in identities:
        station_id = identity["opis_id"]
        row = original[station_id]
        if not any(identity.get(key) for key in ("premises", "aerial_review", "map_review")):
            pending.append({**identity, "coordinate_verified": False})
            continue
        aerial = identity.get("aerial_review")
        map_review = identity.get("map_review")
        if map_review:
            x, y = map_review["pixel_center"]
            width, height = map_review["viewport_pixels"]
            if not 0 <= x < width or not 0 <= y < height:
                raise ValueError(f"Invalid reviewed map pixel for {station_id}")
            if map_review["coordinate_read_from"] != "Google Maps context menu":
                raise ValueError(f"Missing observed map-coordinate evidence for {station_id}")
            feature = {
                "latitude": map_review["latitude"],
                "longitude": map_review["longitude"],
                "tags": {"amenity": "fuel"},
            }
            source_url = map_review["source_url"]
            external_id = f"reviewed-{identity['coordinate_role']}/{station_id}"
        elif aerial:
            sys.path.insert(0, str(DATA.parents[1]))
            from scripts.coordinates.aerial_tiles import BASE, pixel_coordinate

            metadata = json.loads(
                (DATA / "root-manual" / aerial["metadata_file"]).read_text(encoding="utf-8")
            )
            expected_image_id = aerial.get("shared_mosaic_with", station_id)
            if metadata["opis_id"] != expected_image_id:
                raise ValueError(f"Wrong reviewed imagery identity for {station_id}")
            x, y = aerial["pixel_center"]
            left, top, right, bottom = aerial["review_pixel_bounds"]
            width, height = metadata["size"]
            if not 0 <= left < x < right <= width or not 0 <= top < y < bottom <= height:
                raise ValueError(f"Invalid reviewed imagery bounds for {station_id}")
            lat, lon = pixel_coordinate(
                x, y, metadata["left_tile"], metadata["top_tile"], metadata["zoom"]
            )
            source_url = f"{BASE}/{metadata['zoom']}/{metadata['top_tile'] + math.floor(y / 256)}/{metadata['left_tile'] + math.floor(x / 256)}"
            feature = {
                "latitude": lat,
                "longitude": lon,
                "tags": {"amenity": "fuel", "building": "roof"},
            }
            external_id = (
                f"reviewed-{identity['coordinate_role']}/{station_id}"
                if args.distinct_conflicts
                else f"reviewed-canopy/{station_id}"
            )
        else:
            feature_type, feature_id = identity["premises"]
            feature = records[(feature_type, feature_id)]
            if feature["tags"].get("amenity") != "fuel":
                raise ValueError(f"Reviewed feature is not mapped fuel premises: {station_id}")
            source_url = f"https://www.openstreetmap.org/{feature_type}/{feature_id}"
            external_id = f"{feature_type}/{feature_id}"
        evidence = [
            *identity["evidence"],
            {
                "source_url": source_url,
                "reason": identity["reason"],
                "tags": feature["tags"],
            },
        ]
        result = {
            "opis_id": station_id,
            "latitude": feature["latitude"],
            "longitude": feature["longitude"],
            "source": "manually_reviewed_station_premises",
            "source_url": source_url,
            "external_id": external_id,
            "matched_name": identity["matched_name"],
            "matched_address": identity["matched_address"],
            "coordinate_role": identity["coordinate_role"],
            "coordinate_verified": True,
            "operational_status": identity.get("operational_status", "current_operator_directory"),
            "evidence": evidence,
            "original_names": [row["Truckstop Name"]],
            "original_address": row["Address"],
            "original_city": row["City"],
            "original_state": row["State"],
            "rejected_candidates": [],
        }
        if aerial:
            result["aerial_review"] = aerial
            result["coordinate_accuracy_meters"] = identity["coordinate_accuracy_meters"]
        if map_review:
            result["map_review"] = map_review
            result["coordinate_accuracy_meters"] = identity["coordinate_accuracy_meters"]
        for candidate in raw_candidates:
            if candidate["opis_id"] != station_id:
                continue
            if str(candidate.get("external_id")) not in identity.get("rejected_external_ids", []):
                continue
            rejection = {
                "opis_id": station_id,
                "source_url": candidate["source_url"],
                "external_id": str(candidate["external_id"]),
                "latitude": candidate["latitude"],
                "longitude": candidate["longitude"],
                "reason": identity["rejection_reason"],
                "evidence": evidence,
            }
            rejected.append(rejection)
            result["rejected_candidates"].append(rejection)
        accepted.append(result)
    result_kind = (
        "conflict-reviews" if args.distinct_conflicts or args.network_audit else "candidates"
    )
    write_json(DATA / f"{prefix}-{result_kind}.json", accepted)
    write_json(DATA / f"{prefix}-rejected-points.json", rejected)
    write_json(DATA / f"{prefix}-unresolved.json", pending)
    print(
        json.dumps({"accepted": len(accepted), "rejected": len(rejected), "pending": len(pending)})
    )


if __name__ == "__main__":
    main()
