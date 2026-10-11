"""Validate the bounded network-premises review handoff against cached inputs."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from loves_ta import DATA, write_json
from shapely.geometry import Point, shape

PREFIX = "supplemental-network-audit"


def read(suffix: str) -> list[dict]:
    return json.loads((DATA / f"{PREFIX}-{suffix}.json").read_text(encoding="utf-8"))


def key(record: dict) -> tuple:
    return (
        record["opis_id"],
        record["source_url"],
        str(record["external_id"]),
        record["latitude"],
        record["longitude"],
    )


def distance(first: dict, second: dict) -> float:
    lat1, lat2 = map(math.radians, (first["latitude"], second["latitude"]))
    delta_lat = lat2 - lat1
    delta_lon = math.radians(second["longitude"] - first["longitude"])
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 6_371_000 * 2 * math.asin(min(1, math.sqrt(value)))


def main() -> None:
    targets = [r["station"] for r in read("targets")]
    identities = read("identities")
    accepted, rejected, pending = (
        read("conflict-reviews"),
        read("rejected-points"),
        read("unresolved"),
    )
    held = read("held-identities")
    raw = read("raw-candidates")
    target_ids = {r["opis_id"] for r in targets}
    identity_ids = {r["opis_id"] for r in identities}
    accepted_ids = {r["opis_id"] for r in accepted}
    pending_ids = {r["opis_id"] for r in pending}
    if target_ids != identity_ids or len(identity_ids) != len(identities):
        raise ValueError("Missing or duplicated target identities")
    if accepted_ids & pending_ids or accepted_ids | pending_ids != target_ids:
        raise ValueError("Accepted and pending records do not partition the targets")
    if len(accepted_ids) != len(accepted) or len(pending_ids) != len(pending):
        raise ValueError("Duplicated output ID")
    target_keys = {key(r) for r in targets}
    if {r["opis_id"] for r in held} != pending_ids:
        raise ValueError("Held source identities do not cover every pending target")
    if any(key(r) not in target_keys or r["coordinate_verified"] for r in held):
        raise ValueError("Held review does not preserve an exact unverified input")
    raw_keys = {key(r) for r in raw}
    rejected_keys = [key(r) for r in rejected]
    if len(set(rejected_keys)) != len(rejected_keys) or not set(rejected_keys) <= raw_keys:
        raise ValueError("Rejections must identify unique exact cached source records")
    root = Path(__file__).resolve().parents[2]
    state_features = json.loads(
        (root / "trips/data/us-states.geojson").read_text(encoding="utf-8")
    )["features"]
    states = {f["properties"]["STUSAB"]: shape(f["geometry"]) for f in state_features}
    diagnostics = []
    for record in accepted:
        station_id = record["opis_id"]
        review = record["map_review"]
        if not record["coordinate_verified"] or record["coordinate_role"] not in {
            "fuel_island",
            "fuel_canopy",
            "site_forecourt",
            "station_premises",
        }:
            raise ValueError(f"Invalid verified role: {station_id}")
        if record["latitude"] != review["latitude"] or record["longitude"] != review["longitude"]:
            raise ValueError(f"Observed coordinate changed: {station_id}")
        if not states[record["original_state"]].covers(
            Point(record["longitude"], record["latitude"])
        ):
            raise ValueError(f"Coordinate outside original state: {station_id}")
        x, y = review["pixel_center"]
        width, height = review["viewport_pixels"]
        if (
            not 0 <= x < width
            or not 0 <= y < height
            or review["coordinate_read_from"] != "Google Maps context menu"
        ):
            raise ValueError(f"Invalid visible map observation: {station_id}")
        observed = datetime.fromisoformat(review["observed_at"])
        if observed > datetime.now(UTC):
            raise ValueError(f"Observation timestamp is in the future: {station_id}")
        if not record["evidence"] or not record["matched_address"] or not review["observations"]:
            raise ValueError(f"Missing premise identity evidence: {station_id}")
        for evidence in record["evidence"]:
            if urlsplit(evidence["source_url"]).scheme != "https" or not evidence["reason"]:
                raise ValueError(f"Invalid evidence source: {station_id}")
        prior = next(r for r in targets if r["opis_id"] == station_id)
        diagnostics.append(
            {
                "opis_id": station_id,
                "original_network_distance_meters": round(distance(prior, record), 1),
                "coordinate_role": record["coordinate_role"],
                "exact_rejected_records": len(record["rejected_candidates"]),
            }
        )
    for rejection in rejected:
        if not rejection["reason"] or not rejection["evidence"]:
            raise ValueError("Incomplete rejection evidence")
    if any(r["coordinate_verified"] for r in pending):
        raise ValueError("Unresolved property was marked verified")
    report = {
        "targets": len(target_ids),
        "accepted": len(accepted),
        "pending": len(pending),
        "exact_rejected_records": len(rejected),
        "held_source_identities": len(held),
        "validation_errors": [],
        "checks": [
            "complete unique target partition",
            "exact raw record rejections",
            "unchanged observed context-menu coordinates",
            "original US state polygon containment",
            "valid viewport pixels and timestamps",
            "explicit premise evidence and roles",
            "unresolved properties remain unverified",
            "every held identity preserves exact original source and point",
        ],
        "review_access_observation_ids": [71004, 70979],
        "payment_scope": "These two access observations are public reviews, not primary payment eligibility rules. Coordinate verification does not certify fuel grade, current price, or card eligibility.",
        "diagnostics": diagnostics,
    }
    write_json(DATA / f"{PREFIX}-validation-report.json", report)
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "targets",
                    "accepted",
                    "pending",
                    "exact_rejected_records",
                    "validation_errors",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
