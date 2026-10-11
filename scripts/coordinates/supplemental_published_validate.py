"""Validate source-bound station points whose published identities were reviewed."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from shapely.geometry import Point, shape

from scripts.coordinates.aerial_tiles import pixel_coordinate
from scripts.coordinates.match_osm import GEOD, SOURCES, stations
from scripts.coordinates.matching import normalized


def load(filename: str):
    return json.loads((SOURCES / filename).read_text(encoding="utf-8"))


def rejection_errors(records: list[dict]) -> list[str]:
    """Require an exact rejected point and nonempty supporting evidence."""
    errors = []
    for record in records:
        if not isinstance(record.get("evidence"), list) or not all(
            record.get(field) not in (None, "", [])
            for field in (
                "opis_id",
                "source_url",
                "external_id",
                "latitude",
                "longitude",
                "reason",
                "evidence",
            )
        ):
            errors.append(f"Rejected point lacks identity or evidence for {record.get('opis_id')}")
    return errors


def validate_reviews(reviews: list[dict]) -> tuple[list[dict], dict]:
    """Check reviewed identities, raw-source membership and point provenance offline."""
    originals = stations()
    state_features = json.loads(Path("trips/data/us-states.geojson").read_text())["features"]
    states = {f["properties"]["STUSAB"]: shape(f["geometry"]) for f in state_features}
    state_names = {
        f["properties"]["STUSAB"]: f["properties"]["NAME"].lower().replace(" ", "-")
        for f in state_features
    }
    cached = {}

    def source(filename: str):
        if filename not in cached:
            cached[filename] = load(filename)
        return cached[filename]

    candidates, errors, seen = [], [], set()
    for review in reviews:
        identifier = review["opis_id"]
        original = originals[identifier]
        if identifier in seen:
            errors.append(f"Duplicate reviewed ID {identifier}")
        seen.add(identifier)
        for field, csv_field in (
            ("original_names", "names"),
            ("original_address", "Address"),
            ("original_city", "City"),
            ("original_state", "State"),
        ):
            if review[field] != original[csv_field]:
                errors.append(f"Original CSV identity changed for {identifier}")
        native = review["native"]["point"]
        if native not in source(review["native"]["cache"]):
            errors.append(f"Native source differs from cache for {identifier}")
        identity = review["identity_review"]
        if not (
            identity.get("exact_name_and_city")
            and (
                identity.get("numeric_street_agrees") or identity.get("exact_route_property_agrees")
            )
            and identity.get("original_road_evidence")
        ):
            errors.append(f"Missing explicit identity or road proof for {identifier}")
        if identity.get("original_road_conflict"):
            correction = identity.get("individual_source_correction", {})
            if not (
                correction.get("reason")
                and correction.get("original_name_publisher_url", "").startswith("https://")
                and correction.get("primary_numeric_street_url", "").startswith("https://")
                and correction.get("no_competing_original_identity")
            ):
                errors.append(f"Unreviewed original-road contradiction for {identifier}")
        if review.get("held_reason") or native.get("operating_status") == "permanently_closed":
            errors.append(f"Held or closed property cannot be accepted for {identifier}")
        if not review["primary_evidence"]:
            errors.append(f"Missing published primary identity for {identifier}")
        for item in review["primary_evidence"]:
            raw = source(item["cache"])
            record = item["source_record"]
            if record != raw and not (isinstance(raw, list) and record in raw):
                errors.append(f"Primary record differs from cache for {identifier}")
            if record.get("raw_cache"):
                feature = record.get("raw_feature")
                raw_features = source(record["raw_cache"]).get("features", [])
                if feature not in raw_features:
                    errors.append(
                        f"Primary locator feature differs from raw cache for {identifier}"
                    )
        osm = review.get("osm_fuel")
        if osm:
            raw_fuel = source(f"osm-states/{state_names[original['State']]}.json")["records"]
            if osm not in raw_fuel or osm["tags"].get("amenity") != "fuel":
                errors.append(f"Fuel record differs from raw mapped equipment for {identifier}")
            distance = abs(
                GEOD.inv(
                    native["longitude"], native["latitude"], osm["longitude"], osm["latitude"]
                )[2]
            )
            if distance > 100:
                errors.append(f"Native and mapped station points disagree for {identifier}")
        elif not identity.get("published_fuel_evidence"):
            errors.append(f"Native point lacks an exact published fuel identity for {identifier}")
        equipment = review.get("equipment_evidence")
        equipment_valid = False
        if equipment:
            metadata = source(equipment["cache"])
            selected = equipment.get("reviewed_pixel", [])
            if (
                equipment.get("source_record") != metadata
                or metadata.get("opis_id") != identifier
                or equipment.get("image")
                != str(Path(equipment["cache"]).with_suffix(".png")).replace("\\", "/")
                or not (SOURCES / equipment.get("image", "")).is_file()
                or not equipment.get("observation")
                or len(selected) != 2
                or not all(0 <= selected[i] < metadata["size"][i] for i in range(2))
            ):
                errors.append(f"Missing source-bound imagery review for {identifier}")
            else:
                image_lat, image_lon = pixel_coordinate(
                    *selected, metadata["left_tile"], metadata["top_tile"], metadata["zoom"]
                )
                equipment_valid = (
                    abs(GEOD.inv(image_lon, image_lat, review["longitude"], review["latitude"])[2])
                    < 2
                )
                if not equipment_valid:
                    errors.append(
                        f"Reviewed premises pixel differs from chosen point for {identifier}"
                    )
        primary_point = review.get("primary_fuel_point")
        primary_point_valid = False
        if primary_point:
            feature = primary_point["source_record"]
            properties = feature.get("properties", {})
            coordinates = feature.get("geometry", {}).get("coordinates", [])
            native_street = native.get("addresses", [{}])[0].get("freeform", "") or ""
            primary_number = re.match(r"\d+", properties.get("addr:street_address", ""))
            native_number = re.match(r"\d+", native_street)
            primary_point_valid = bool(
                feature in source(primary_point["cache"]).get("features", [])
                and properties.get("amenity") == "fuel"
                and properties.get("addr:state") == original["State"]
                and normalized(properties.get("addr:city", "")) == normalized(original["City"])
                and primary_number
                and native_number
                and primary_number.group() == native_number.group()
                and len(coordinates) == 2
                and abs(GEOD.inv(native["longitude"], native["latitude"], *coordinates)[2]) <= 100
            )
            if not primary_point_valid:
                errors.append(
                    f"Primary fuel point lacks exact premises corroboration for {identifier}"
                )
        if not osm and native.get("category") not in {"gas_station", "convenience_store"}:
            errors.append(
                f"Native category is not a reviewed fuel or convenience premise for {identifier}"
            )
        if (
            not osm
            and native.get("category") != "gas_station"
            and not equipment_valid
            and not primary_point_valid
        ):
            network = review.get("fuel_network_evidence", {})
            record = network.get("source_record", {})
            native_street = native.get("addresses", [{}])[0].get("freeform", "")
            if (
                native.get("category") != "convenience_store"
                or not network
                or record not in source(network["cache"])
                or "fuel" not in record.get("tags", [])
                or normalized(record.get("address1", "")) != normalized(native_street or "")
                or normalized(record.get("city", "")) != normalized(original["City"])
                or record.get("stateCode") != original["State"]
                or abs(
                    GEOD.inv(
                        native["longitude"],
                        native["latitude"],
                        record.get("lng", 0),
                        record.get("lat", 0),
                    )[2]
                )
                > 100
            ):
                errors.append(
                    f"Convenience-only native point lacks exact independent fuel-network corroboration for {identifier}"
                )
        origin = review["origin"]
        directory = review.get("directory_point")
        chosen = (
            osm
            if origin == "published_osm_station"
            else directory["source_record"]
            if directory and origin == "published_directory_station"
            else native
        )
        if directory:
            raw_directory = source(directory["cache"])
            if directory["source_record"] not in raw_directory:
                errors.append(f"Published directory record differs from cache for {identifier}")
            published = directory["source_record"]
            if published.get("raw_json_cache"):
                raw_record = published.get("raw_record")
                if raw_record not in source(published["raw_json_cache"]) or any(
                    published.get(field) != raw_record.get(field)
                    for field in ("latitude", "longitude", "external_id", "source_url")
                ):
                    errors.append(
                        f"Published station point differs from raw directory record for {identifier}"
                    )
            if published.get("raw_html_cache"):
                html = (SOURCES / published["raw_html_cache"]).read_text(encoding="utf-8")
                points = {
                    (float(lat), float(lon))
                    for lat, lon in re.findall(
                        r'"latitude"\s*:\s*([\d.-]+)\s*,\s*"longitude"\s*:\s*([\d.-]+)',
                        html,
                    )
                }
                if (published["latitude"], published["longitude"]) not in points:
                    errors.append(
                        f"Published station point differs from primary page for {identifier}"
                    )
        if origin == "published_directory_station" and not (
            directory
            and equipment_valid
            and review.get("native_point_rejected")
            and any(
                r.get("external_id") == native["external_id"]
                and r.get("latitude") == native["latitude"]
                and r.get("longitude") == native["longitude"]
                for r in review.get("rejected_candidates", [])
            )
        ):
            errors.append(
                f"Corrected directory point lacks physical premises or wrong-pin exclusion for {identifier}"
            )
        if (
            origin
            not in {
                "published_osm_station",
                "published_native_station",
                "published_directory_station",
            }
            or chosen is None
        ):
            errors.append(f"Unknown published point source for {identifier}")
        elif (review["latitude"], review["longitude"]) != (chosen["latitude"], chosen["longitude"]):
            errors.append(f"Published coordinate was altered for {identifier}")
        if not states[original["State"]].covers(Point(review["longitude"], review["latitude"])):
            errors.append(f"Published point is outside original state for {identifier}")
        if review["coordinate_role"] != "station_premises":
            errors.append(
                f"Published point cannot claim an individual fuel canopy for {identifier}"
            )
        if datetime.fromisoformat(review["reviewed_at"]) > datetime.now(UTC):
            errors.append(f"Review timestamp is in the future for {identifier}")
        candidate = {
            key: review[key]
            for key in (
                "opis_id",
                "latitude",
                "longitude",
                "source_url",
                "external_id",
                "matched_name",
                "matched_address",
                "coordinate_role",
                "operational_status",
                "original_names",
                "original_address",
                "original_city",
                "original_state",
            )
        }
        candidate.update(
            source="reviewed_published_station_point",
            coordinate_verified=True,
            evidence=[
                *review["primary_evidence"],
                {
                    "source_url": native["source_url"],
                    "cache": review["native"]["cache"],
                    "source_record": native,
                    "reason": review["reason"],
                    "identity_review": identity,
                    "reviewed_at": review["reviewed_at"],
                },
            ],
            rejected_candidates=review.get("rejected_candidates", []),
        )
        if osm:
            candidate["evidence"].append(
                {
                    "source_url": f"https://www.openstreetmap.org/{osm['type']}/{osm['id']}",
                    "source_record": osm,
                    "reason": "Independent actual fuel mapping corroborates the exact reviewed station premises.",
                }
            )
        if review.get("fuel_network_evidence"):
            candidate["evidence"].append(review["fuel_network_evidence"])
        if equipment:
            candidate["evidence"].append(equipment)
        if primary_point:
            candidate["evidence"].append(primary_point)
        if directory:
            candidate["evidence"].append(directory)
        candidates.append(candidate)
    return candidates, {
        "reviewed_ids": len(reviews),
        "accepted_ids": len(candidates) if not errors else 0,
        "network_calls": 0,
        "errors": errors,
        "note": "Published station points retain their exact source coordinates. Identity is individually reviewed; no manual canopy selection or current price/availability certification is claimed.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch",
        required=True,
        choices=(
            "ninth",
            "tenth",
            "eleventh",
            "twelfth",
            "thirteenth",
            "fourteenth",
            "fifteenth",
            "sixteenth",
            "seventeenth",
        ),
    )
    args = parser.parse_args()
    stem = f"supplemental-independent-{args.batch}"
    candidates, report = validate_reviews(load(f"{stem}-reviews.json"))
    report["errors"].extend(rejection_errors(load(f"{stem}-rejected-points.json")))
    if report["errors"]:
        report["accepted_ids"] = 0
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
