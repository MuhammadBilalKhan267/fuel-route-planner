"""Validate and emit individually reviewed independent fuel premises."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from shapely.geometry import Point, shape

from scripts.coordinates.match_osm import GEOD, SOURCES, stations


def load(filename: str):
    return json.loads((SOURCES / filename).read_text(encoding="utf-8"))


def validate_reviews(reviews: list[dict]) -> tuple[list[dict], dict]:
    """Check source integrity and manual-review metadata without writing artifacts."""
    original = stations()
    features = json.loads(Path("trips/data/us-states.geojson").read_text())["features"]
    states = {feature["properties"]["STUSAB"]: shape(feature["geometry"]) for feature in features}
    state_names = {
        feature["properties"]["STUSAB"]: feature["properties"]["NAME"].lower().replace(" ", "-")
        for feature in features
    }
    fuel_records = {}
    for state in {review["original_state"] for review in reviews}:
        raw = load(f"osm-states/{state_names[state]}.json")
        fuel_records[state] = {(point["type"], point["id"]): point for point in raw["records"]}
    candidates, errors, seen = [], [], set()
    now = datetime.now(UTC)
    for review in reviews:
        identifier = review["opis_id"]
        if identifier in seen:
            errors.append(f"Duplicate reviewed OPIS ID {identifier}")
        seen.add(identifier)
        row = original[identifier]
        if (
            review["original_names"] != row["names"]
            or review["original_address"] != row["Address"]
            or review["original_city"] != row["City"]
            or review["original_state"] != row["State"]
        ):
            errors.append(f"Original CSV identity changed for {identifier}")
        pair = review["named_pair"]
        osm, native = pair["osm_fuel"], pair["native_poi"]
        tags = osm["tags"] if osm else {}
        primary_manual_identity = (
            review.get("identity_method") == "current_primary_named_property"
            and review["origin"] == "manual_context_menu"
            and bool(review["primary_evidence"])
            and bool(pair["original_exit_evidence"] or review.get("original_road_corroborated"))
        )
        if osm:
            if fuel_records[row["State"]].get((osm["type"], osm["id"])) != osm:
                errors.append(f"Fuel source differs from raw mapped record for {identifier}")
            if tags.get("amenity") != "fuel":
                errors.append(f"Mapped source is not actual fuel equipment for {identifier}")
            if not (tags.get("name") or tags.get("operator") or primary_manual_identity):
                errors.append(f"Unnamed equipment lacks reviewed primary identity for {identifier}")
        elif not primary_manual_identity:
            errors.append(
                f"Missing mapped equipment and reviewed primary identity for {identifier}"
            )
        cached_native = load(pair["native_cache"])
        if native not in cached_native:
            errors.append(f"Native source record differs from cache for {identifier}")
        for item in review["primary_evidence"]:
            if "cache" in item and "source_record" in item:
                cached = load(item["cache"])
                if item["source_record"] != cached and not (
                    isinstance(cached, list) and item["source_record"] in cached
                ):
                    errors.append(f"Primary identity source differs from cache for {identifier}")
                record = item["source_record"]
                if isinstance(record, dict) and {"raw_cache", "raw_feature"} <= record.keys():
                    raw_primary = load(record["raw_cache"])
                    if record["raw_feature"] not in raw_primary["features"]:
                        errors.append(
                            f"Derived primary feature differs from cache for {identifier}"
                        )
        if pair["legacy_number_unconfirmed"]:
            errors.append(f"Unproven legacy branch number for {identifier}")
        if not pair["original_exit_evidence"] and not review.get("original_road_corroborated"):
            errors.append(f"Original road or exit is not corroborated for {identifier}")
        lat, lon = review["latitude"], review["longitude"]
        if not states[row["State"]].covers(Point(lon, lat)):
            errors.append(f"Coordinate is outside CSV state for {identifier}")
        anchor = osm or native
        separation = abs(GEOD.inv(lon, lat, anchor["longitude"], anchor["latitude"])[2])
        if separation > (100 if osm else 200):
            errors.append(
                f"Reviewed equipment is too far from the reviewed premises anchor for {identifier}"
            )
        if review["origin"] == "osm_mapped_roof":
            if (
                not osm
                or tags.get("building") != "roof"
                or (lat, lon)
                != (
                    osm["latitude"],
                    osm["longitude"],
                )
            ):
                errors.append(f"Published roof coordinate differs for {identifier}")
        elif review["origin"] == "manual_context_menu":
            if review.get("context") != [lat, lon]:
                errors.append(f"Selected menu coordinate differs for {identifier}")
        else:
            errors.append(f"Unknown coordinate origin for {identifier}")
        width, height = review["viewport"]
        x, y = review["pixel"]
        if not (0 <= x < width and 0 <= y < height):
            errors.append(f"Selected pixel is outside viewport for {identifier}")
        if datetime.fromisoformat(review["reviewed_at"]) > now:
            errors.append(f"Review timestamp is in the future for {identifier}")
        source_url = review["url"]
        external_id = f"manual-premises:{identifier}"
        if osm:
            source_url = f"https://www.openstreetmap.org/{osm['type']}/{osm['id']}"
            external_id = f"{osm['type']}/{osm['id']}"
        if review["origin"] == "manual_context_menu":
            source_url = review["url"]
            external_id = f"manual-premises:{identifier}"
        evidence = [
            {
                "source_url": native["source_url"],
                "reason": "Distinctive same-city native place identity agrees with the individually reviewed property; the native pin alone is not accepted.",
                "source_record": native,
                "cache": pair["native_cache"],
            },
            *review["primary_evidence"],
            {
                "source_url": review["url"],
                "reason": review["reason"],
                "reviewed_at": review["reviewed_at"],
                "viewport": review["viewport"],
                "selected_pixel": review["pixel"],
                "visible_context_coordinate": review.get("context"),
                "screenshot_retention": "Displayed in browser tool history; no local Google image file is claimed.",
            },
        ]
        if osm:
            evidence.insert(
                0,
                {
                    "source_url": f"https://www.openstreetmap.org/{osm['type']}/{osm['id']}",
                    "reason": "Independently mapped fuel equipment corroborates the accepted property; its operator binding is documented by the individual identity review.",
                    "source_record": osm,
                },
            )
        for anchor in pair["original_exit_evidence"]:
            evidence.append(
                {
                    "source_url": f"https://www.openstreetmap.org/node/{anchor['junction_id']}",
                    "reason": "Published original route and numbered exit corroborates the same distinctive nearby property; an exit is a search anchor, not a fuel coordinate.",
                    "source_record": anchor,
                }
            )
        candidates.append(
            {
                "opis_id": identifier,
                "latitude": lat,
                "longitude": lon,
                "source": "manually_reviewed_station_premises",
                "source_url": source_url,
                "external_id": external_id,
                "matched_name": review["name"],
                "matched_address": review["address"],
                "coordinate_role": review["coordinate_role"],
                "coordinate_verified": True,
                "operational_status": review["operational_status"],
                "evidence": evidence,
                "original_names": row["names"],
                "original_address": row["Address"],
                "original_city": row["City"],
                "original_state": row["State"],
                "rejected_candidates": [],
            }
        )
    report = {
        "reviewed_ids": len(reviews),
        "accepted_ids": len(candidates) if not errors else 0,
        "network_calls": 0,
        "errors": errors,
        "note": "Individual visual and identity reviews precede these structural checks. Coordinates do not certify fuel grade, current price, card eligibility or operating continuity.",
    }
    return candidates, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--second", action="store_true", help="Validate the second reviewed batch.")
    group.add_argument("--third", action="store_true", help="Validate the third reviewed batch.")
    group.add_argument(
        "--batch",
        choices=("name", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth"),
        default="name",
    )
    args = parser.parse_args()
    batch = "third" if args.third else "second" if args.second else args.batch
    stem = f"supplemental-independent-{batch}"
    candidates, report = validate_reviews(load(f"{stem}-reviews.json"))
    (SOURCES / f"{stem}-validation-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    if report["errors"]:
        raise SystemExit(json.dumps(report))
    (SOURCES / f"{stem}-candidates.json").write_text(
        json.dumps(candidates, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(report))


if __name__ == "__main__":
    main()
