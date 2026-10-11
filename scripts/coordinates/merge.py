"""Audit independent station sources and prepare a bound, offline coordinate import."""

import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from pyproj import Geod
from shapely import make_valid, prepare
from shapely.geometry import Point, shape

from scripts.coordinates.match_osm import stations

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "data/coordinate-sources"
GEOD = Geod(ellps="WGS84")

# Lower ranks prefer coordinates published by the station operator.
SOURCE_RANK = {
    "manually_reviewed_station_premises": -1,
    "loves_official_store": 0,
    "ta_petro_official_store": 0,
    "pilot_official_locator": 0,
    "pilot_official_locator_manual_identity": 0,
    "official_locator_routable_coordinate": 0,
    "official_locator_display_coordinate": 1,
    "official_locator_station_coordinate": 1,
    "official_published_store_table": 1,
    "current_official_locator_crosschecked_archived_store_id": 1,
    "official_locator_via_alltheplaces": 2,
    "osm_station_poi": 3,
    "catscale_official_location": 4,
    "pilot_official_2022_network_list": 5,
    "pilot_official_archived_network_list": 6,
    "ambest_official_location": 1,
    "road_ranger_official_location": 0,
    "road_ranger_official_billing_map": 0,
    "sapp_bros_official_location": 0,
    "fuelman_public_station_locator": 3,
    "cfn_public_station_locator": 3,
    "roadys_official_locator_via_alltheplaces": 2,
    "official_7fleet_current_crosschecked_historical_store_id": 1,
    "current_maverik_crosschecked_historical_kum_and_go_address": 1,
    "quickfuel_official_locator": 0,
    "operator_locator_with_station_identity": 1,
    "osm_premises_with_published_address": 1,
    "operator_premises_with_published_address": 1,
    "federal_gps_premises_with_published_address": 1,
    "mapped_named_premises_with_published_street_identity": 1,
    "official_usda_snap_retailer_coordinate": 4,
    "historical_usda_snap_retailer_coordinate": 6,
    "current_official_maverik_crosschecked_historical_kum_and_go_store_id": 1,
}


def distance(first, second):
    return abs(
        GEOD.inv(first["longitude"], first["latitude"], second["longitude"], second["latitude"])[2]
    )


def rejected_candidates(candidates, review):
    if review["source"] != "manually_reviewed_station_premises":
        return set()
    descriptions = [
        item
        for item in review.get("rejected_candidates", [])
        if item.get("reason") and item.get("source_url", "").startswith("https://")
    ]
    rejected = {
        index
        for index, item in enumerate(candidates)
        if item["source"] != "manually_reviewed_station_premises"
        and any(
            item["source_url"] == description["source_url"]
            and (
                not description.get("external_id")
                or str(item["external_id"]) == str(description["external_id"])
            )
            and (
                not {"latitude", "longitude"} <= description.keys()
                or distance(item, description) < 1
            )
            for description in descriptions
        )
    }
    corpay_sources = {"fuelman_public_station_locator", "cfn_public_station_locator"}
    # Both network views publish the same backend records. A reviewed rejection
    # also applies to the identical record and point in the other view.
    originals = [candidates[index] for index in rejected]
    for index, item in enumerate(candidates):
        if item["source"] in corpay_sources and any(
            original["source"] in corpay_sources
            and item["external_id"] == original["external_id"]
            and distance(item, original) < 1
            for original in originals
        ):
            rejected.add(index)
    return rejected


def source_identity(item):
    if item["source"] in {"fuelman_public_station_locator", "cfn_public_station_locator"}:
        return "https://sitelocator.fuelman.com/bin/sl/search", item["external_id"]
    return item["source_url"], item["external_id"]


def reconcile(candidates):
    ordered = sorted(
        candidates,
        key=lambda item: (
            SOURCE_RANK.get(item["source"], 2),
            item["source_url"],
            item["external_id"],
        ),
    )
    selected = ordered[0]
    rejected = rejected_candidates(ordered, selected)
    disagreements = [
        {"source": item["source"], "distance_meters": round(distance(selected, item), 1)}
        for index, item in enumerate(ordered[1:], 1)
        if distance(selected, item) > 600 and index not in rejected
    ]
    if disagreements:
        return None, disagreements
    retained = [selected] + [
        item
        for index, item in enumerate(ordered[1:], 1)
        if distance(selected, item) <= 600 and index not in rejected
    ]
    statuses = {item.get("availability", "unknown") for item in retained}
    selected = dict(selected)
    selected["operational_status"] = "closed" if "closed" in statuses else "unknown"
    independent = {}
    for item in retained:
        independent.setdefault(source_identity(item), item)
    selected["corroborating_sources"] = [
        {
            "source": item["source"],
            "source_url": item["source_url"],
            "distance_meters": round(distance(selected, item), 1),
        }
        for item in list(independent.values())[1:]
    ]
    selected["reviewed_rejections"] = [
        {
            "source": ordered[index]["source"],
            "source_url": ordered[index]["source_url"],
            "external_id": ordered[index]["external_id"],
        }
        for index in sorted(rejected)
    ]
    return selected, []


def identity_collisions(grouped, aliases):
    identities = defaultdict(set)
    for identifier, candidates in grouped.items():
        rejected = set().union(*(rejected_candidates(candidates, item) for item in candidates))
        for index, candidate in enumerate(candidates):
            if index not in rejected:
                identities[source_identity(candidate)].add(identifier)
    collisions = defaultdict(list)
    for (url, external_id), identifiers in identities.items():
        if len({aliases.get(identifier, identifier) for identifier in identifiers}) > 1:
            issue = {"source_url": url, "external_id": external_id, "opis_ids": sorted(identifiers)}
            for identifier in identifiers:
                collisions[identifier].append(issue)
    return collisions


def physical_aliases(sites, rows):
    aliases = {}
    for site in sites:
        identifiers = site["opis_ids"]
        canonical = site["canonical_opis_id"]
        if not site.get("evidence") or not site.get("source_url", "").startswith("https://"):
            raise ValueError("Physical station aliases require reviewed identity evidence.")
        if len(set(identifiers)) < 2 or canonical not in identifiers:
            raise ValueError("A physical alias must include at least two IDs and its canonical ID.")
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("A physical alias cannot repeat a station ID.")
        if any(identifier in aliases or identifier not in rows for identifier in identifiers):
            raise ValueError("Duplicate or unknown physical station alias.")
        if len({rows[identifier]["State"] for identifier in identifiers}) != 1:
            raise ValueError("Physical station aliases must share a state.")
        for identifier in identifiers:
            aliases[identifier] = canonical
    return aliases


def unresolved_identity_reviews(reviews, rows):
    indexed = {}
    for review in reviews:
        identifier = review["opis_id"]
        row = rows.get(identifier)
        if identifier in indexed or row is None:
            raise ValueError("Duplicate or unknown unresolved identity review.")
        if not review.get("reason") or not review.get("source_url", "").startswith("https://"):
            raise ValueError(
                "Unresolved identity reviews require an explanation and public source."
            )
        if any(
            review.get(original) != row[column]
            for original, column in (
                ("original_address", "Address"),
                ("original_city", "City"),
                ("original_state", "State"),
            )
        ):
            raise ValueError("Unresolved identity review does not match the original CSV location.")
        indexed[identifier] = review
    return indexed


def reviewed_point_rejections(reviews, rows):
    indexed = defaultdict(list)
    for review in reviews:
        identifier = review["opis_id"]
        if identifier not in rows:
            raise ValueError("Unknown station ID in a rejected point review.")
        if (
            not review.get("reason")
            or not review.get("source_url", "").startswith("https://")
            or not review.get("evidence")
            or not review.get("external_id")
            or not all(math.isfinite(review[key]) for key in ("latitude", "longitude"))
        ):
            raise ValueError("A rejected point requires its exact identity, point, and evidence.")
        indexed[identifier].append(review)
    return indexed


def main():
    rows = stations()
    states = {
        feature["properties"]["STUSAB"]: make_valid(shape(feature["geometry"]))
        for feature in json.loads((ROOT / "trips/data/us-states.geojson").read_text())["features"]
    }
    for boundary in states.values():
        prepare(boundary)
    grouped = defaultdict(list)
    aliases = {}
    alias_path = SOURCES / "duplicate-sites.json"
    if alias_path.exists():
        aliases = physical_aliases(json.loads(alias_path.read_text(encoding="utf-8")), rows)
    review_path = SOURCES / "unresolved-identity-reviews.json"
    identity_reviews = unresolved_identity_reviews(
        json.loads(review_path.read_text()) if review_path.exists() else [], rows
    )
    rejections = reviewed_point_rejections(
        [
            item
            for path in SOURCES.glob("*-rejected-points.json")
            for item in json.loads(path.read_text())
        ],
        rows,
    )
    invalid = []
    paths = list(SOURCES.glob("*-candidates.json")) + list(SOURCES.glob("*-conflict-reviews.json"))
    for path in sorted(paths):
        for item in json.loads(path.read_text(encoding="utf-8")):
            identifier = int(item["opis_id"])
            row = rows.get(identifier)
            error = None
            if not row or row["State"] not in states:
                error = "Unknown US CSV station ID"
            elif not all(math.isfinite(item[key]) for key in ("latitude", "longitude")):
                error = "Non-finite coordinate"
            elif not states[row["State"]].covers(Point(item["longitude"], item["latitude"])):
                error = "Coordinate is outside the CSV station state"
            elif not item.get("source_url", "").startswith("https://") or not item.get("evidence"):
                error = "Missing source URL or station identity evidence"
            if error:
                invalid.append({"file": path.name, "opis_id": identifier, "error": error})
            else:
                grouped[identifier].append(item)
    for identifier, reviews in rejections.items():
        candidates = grouped[identifier]
        rejected = rejected_candidates(
            candidates,
            {"source": "manually_reviewed_station_premises", "rejected_candidates": reviews},
        )
        for index in sorted(rejected):
            invalid.append(
                {
                    "opis_id": identifier,
                    "source_url": candidates[index]["source_url"],
                    "external_id": candidates[index]["external_id"],
                    "error": "Point was explicitly rejected by a premises review.",
                }
            )
        grouped[identifier] = [
            item for index, item in enumerate(candidates) if index not in rejected
        ]
    accepted, conflicts, unresolved = [], [], []
    collisions = identity_collisions(grouped, aliases)
    for identifier, row in rows.items():
        if row["State"] not in states:
            continue
        candidates = grouped.get(identifier, [])
        if not candidates or identifier in collisions or identifier in identity_reviews:
            unresolved.append(
                {
                    "opis_id": identifier,
                    "names": row["names"],
                    "address": row["Address"],
                    "city": row["City"],
                    "state": row["State"],
                    "identity_collisions": collisions.get(identifier, []),
                    "identity_review": identity_reviews.get(identifier),
                }
            )
            continue
        selected, disagreements = reconcile(candidates)
        if selected is None:
            conflicts.append(
                {"opis_id": identifier, "disagreements": disagreements, "candidates": candidates}
            )
            continue
        selected.update(
            original_names=row["names"],
            original_address=row["Address"],
            original_city=row["City"],
            original_state=row["State"],
            site_id=aliases.get(identifier),
        )
        accepted.append(selected)
    for name, records in (
        ("merged", accepted),
        ("conflicts", conflicts),
        ("unresolved", unresolved),
        ("invalid", invalid),
        ("identity-conflicts", dict(collisions)),
    ):
        (SOURCES / f"{name}.json").write_text(
            json.dumps(records, indent=2) + "\n", encoding="utf-8"
        )
    with (ROOT / "data/verified-station-coordinates.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "opis_id",
                "latitude",
                "longitude",
                "source",
                "address",
                "city",
                "state",
                "verified",
                "operational_status",
                "site_id",
            ],
        )
        writer.writeheader()
        for item in sorted(accepted, key=lambda value: value["opis_id"]):
            writer.writerow(
                {
                    "opis_id": item["opis_id"],
                    "latitude": item["latitude"],
                    "longitude": item["longitude"],
                    "source": item["source"],
                    "address": item["original_address"],
                    "city": item["original_city"],
                    "state": item["original_state"],
                    "verified": "true",
                    "operational_status": item["operational_status"],
                    "site_id": item["site_id"],
                }
            )
    print(
        {
            "verified_stations": len(accepted),
            "conflicting_stations": len(conflicts),
            "unresolved_stations": len(unresolved),
            "invalid_candidates": len(invalid),
            "identity_conflicts": len(collisions),
            "sources": dict(Counter(item["source"] for item in accepted)),
            "closed_stations": sum(item["operational_status"] == "closed" for item in accepted),
        }
    )


if __name__ == "__main__":
    main()
