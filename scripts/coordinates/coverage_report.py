"""Explain accepted station coverage without counting one station more than once."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "data/coordinate-sources"

# These describe the selected coordinate, rather than every source used to find it.
METHODS = {
    "operator": (
        "Operator or station-network point",
        (
            "Match the CSV station number or distinctive name and its location to a published "
            "store record. Use that record's station, display, or routing point. The identity "
            "checks also use the street, city/state, or highway/exit when available."
        ),
        {
            "loves_official_store": "Love's public store directory",
            "ta_petro_official_store": "TA/Petro public store directory",
            "pilot_official_locator": "Pilot/Flying J public locator",
            "pilot_official_locator_manual_identity": "Pilot locator with reviewed identity",
            "official_locator_routable_coordinate": "Operator's published routing point",
            "official_locator_display_coordinate": "Operator's published map display point",
            "official_locator_station_coordinate": "Operator's published station point",
            "official_published_store_table": "Operator's downloadable numbered store table",
            "ambest_official_location": "AMBEST public station directory",
            "road_ranger_official_location": "Road Ranger public directory",
            "road_ranger_official_billing_map": "Road Ranger billing map",
            "sapp_bros_official_location": "Sapp Bros public directory",
            "quickfuel_official_locator": "QuickFuel public locator",
            "operator_locator_with_station_identity": "Operator point matched to station identity",
            "official_regional_locator_with_published_station_identity": (
                "Regional operator point with published street identity"
            ),
            "operator_premises_with_published_address": (
                "Operator premises point with published numeric street"
            ),
        },
    ),
    "operator_snapshot": (
        "Explicitly labeled operator-directory extract",
        (
            "Use a dated AllThePlaces extract of an operator's own location feed. Match the "
            "station identity and locality. Keep the original operator URL and snapshot "
            "reference. An extract is a delivery mechanism, not an independent second source."
        ),
        {
            "official_locator_via_alltheplaces": "Operator directory via AllThePlaces",
            "roadys_official_locator_via_alltheplaces": "Roady's directory via AllThePlaces",
        },
    ),
    "historical_identity": (
        "Historical identity bridge to an operator point",
        (
            "Join the original station number/name to its published street in an older "
            "directory or government retailer record. Match that same street to the current "
            "operator. This handles renumbering and rebranding without assuming that similar "
            "store numbers or nearby branches are interchangeable."
        ),
        {
            "official_7fleet_current_crosschecked_historical_store_id": (
                "Current 7Fleet record joined to historical store number"
            ),
            "current_official_maverik_crosschecked_historical_kum_and_go_store_id": (
                "Current Maverik record joined to historical Kum & Go store"
            ),
            "current_maverik_crosschecked_historical_kum_and_go_address": (
                "Current Maverik record joined to historical Kum & Go address"
            ),
            "current_official_locator_crosschecked_archived_store_id": (
                "Current locator joined to archived operator store ID"
            ),
            "official_locator_crosschecked_usda_store_id_address_and_coordinate": (
                "Current locator corroborated by USDA number, street, and point"
            ),
            "official_current_premises_with_historical_identity": (
                "Current operator premises with historical name/address bridge"
            ),
        },
    ),
    "manual_premises": (
        "Premises review and conflict resolution",
        (
            "Reconcile station identities and conflicting points using scripted source checks "
            "and individual property reviews. Some records document imagery inspection or "
            "a selected canopy point; the source label does not establish manual inspection."
        ),
        {"manually_reviewed_station_premises": "Reviewed station premises"},
    ),
    "reviewed_published": (
        "Published point after property review",
        (
            "Resolve the original identity using published station records. Read the exact "
            "named map listing and inspect the property. Retain its published coordinate "
            "unchanged rather than estimating a new point from imagery."
        ),
        {"reviewed_published_station_point": "Reviewed published station point"},
    ),
    "mapped_address": (
        "Published street matched to mapped premises",
        (
            "Obtain a numeric street address from a station, fleet, or government directory. "
            "Match it and the station identity to an OpenStreetMap fuel, shop, or named premises "
            "feature. Use that feature's coordinate, not the street-geocoder search anchor. "
            "The record preserves which kind of feature supplied the point."
        ),
        {
            "osm_premises_with_published_address": "OSM premises with published street",
            "mapped_named_premises_with_published_street_identity": (
                "Named mapped premises with published street identity"
            ),
            "official_numbered_address_on_mapped_fuel_premises": (
                "Official numbered street matched to mapped fuel premises"
            ),
            "mapped_fuel_premises_with_published_street_identity": (
                "Mapped fuel premises with published street identity"
            ),
            "mapped_fuel_with_published_street_identity": (
                "Mapped fuel feature with published street identity"
            ),
            "independent_identity_crosschecked_public_directories": (
                "Mapped premises corroborated by public station directories"
            ),
        },
    ),
    "fuel_network": (
        "Fleet-fuel locator point",
        (
            "Match station name or store number and locality to a CFN/Fuelman diesel station "
            "record. Use its station coordinate after reconciliation. Both locators share a "
            "backend, so their agreement does not count as independent corroboration."
        ),
        {
            "cfn_public_station_locator": "CFN public diesel locator",
            "fuelman_public_station_locator": "Fuelman public diesel locator",
            "reviewed_fuel_network_point": "Fleet-fuel point with separate premises review",
        },
    ),
    "osm_identity": (
        "Direct OpenStreetMap station match",
        (
            "Match a distinctive station name or brand plus store number to a mapped fuel "
            "feature in the correct state and locality. Reject ambiguous matches. City "
            "boundaries or city search anchors constrain the search; their centroids are not "
            "used as the station point."
        ),
        {"osm_station_poi": "Direct OSM station point or fuel-feature geometry"},
    ),
    "federal_gps": (
        "Federal station GPS record",
        (
            "Match a published station street to an Alternative Fuels Data Center premises "
            "record. Use that station's GPS coordinate. This is different from accepting a "
            "generic USDA retailer or EPA tank coordinate without premises checks."
        ),
        {"federal_gps_premises_with_published_address": "AFDC station GPS premises"},
    ),
    "scale": (
        "CAT Scale host-station property",
        (
            "Match CAT Scale's named host station, city/state, and road/exit. Use the "
            "published scale coordinate on that station property. It locates the truck-stop "
            "premises, not necessarily its fuel canopy or entrance."
        ),
        {"catscale_official_location": "CAT Scale host-station location"},
    ),
    "archive": (
        "Archived operator-network coordinate",
        (
            "Match the original numbered station and city/state to an archived Pilot network "
            "list. Retain its published coordinate when accepted by reconciliation. The source "
            "year is preserved; these few points have greater freshness uncertainty."
        ),
        {
            "pilot_official_2022_network_list": "Pilot 2022 network list",
            "pilot_official_archived_network_list": "Older archived Pilot network list",
        },
    ),
}

SOURCE_METHOD = {source: key for key, (_, _, sources) in METHODS.items() for source in sources}
CENSUS_BATCHES = (
    "street-census-anchors.json",
    "directory-street-census-anchors.json",
    "network-audit-census-anchors.json",
    "supplemental-quarles-census-anchors.json",
    "supplemental-remaining-census-anchors.json",
)


def record_key(item):
    return (
        int(item["opis_id"]),
        item["source"],
        item["source_url"],
        str(item["external_id"]),
        float(item["latitude"]),
        float(item["longitude"]),
    )


def indexed(records, label):
    result = {int(item["opis_id"]): item for item in records}
    if len(result) != len(records):
        raise ValueError(f"Duplicate station IDs in {label}.")
    return result


def build_report(rows, states, accepted, unresolved, conflicts, exported, origins, baseline):
    """Require complete, disjoint accounting and agreement with the import artifact."""
    us_rows = [row for row in rows if row["State"].strip().upper() in states]
    us_ids = {int(row["OPIS Truckstop ID"]) for row in us_rows}
    source_rows = {int(row["OPIS Truckstop ID"]): row for row in us_rows}
    selected = indexed(accepted, "accepted records")
    pending = indexed(unresolved, "unresolved records")
    disputed = indexed(conflicts, "coordinate conflicts")
    partition = set(selected) | set(pending) | set(disputed)
    if (
        partition != us_ids
        or set(selected) & set(pending)
        or set(selected) & set(disputed)
        or set(pending) & set(disputed)
    ):
        raise ValueError("Accepted, unresolved, and conflicting IDs must partition US station IDs.")
    imported = indexed(exported, "coordinate export")
    if set(imported) != set(selected):
        raise ValueError("Coordinate export and accepted station IDs differ.")

    source_counts = Counter()
    retained_sources = defaultdict(set)
    origin_ids = defaultdict(set)
    method_ids = defaultdict(list)
    excluded = []
    unknown_status = 0
    routable_sites = set()
    for identifier, item in selected.items():
        source = item["source"]
        if source not in SOURCE_METHOD:
            raise ValueError(f"Coordinate source needs an explicit explanation: {source}")
        if any(
            item[original] != source_rows[identifier][column]
            for column, original in (
                ("Address", "original_address"),
                ("City", "original_city"),
                ("State", "original_state"),
            )
        ):
            raise ValueError(
                f"Accepted coordinate has a stale source-address binding: {identifier}."
            )
        export = imported[identifier]
        if (
            export["source"] != source
            or export["verified"].strip().casefold() != "true"
            or float(export["latitude"]) != float(item["latitude"])
            or float(export["longitude"]) != float(item["longitude"])
            or export["operational_status"] != item["operational_status"]
            or int(export.get("site_id") or identifier) != int(item.get("site_id") or identifier)
            or any(
                export[column] != item[original]
                for column, original in (
                    ("address", "original_address"),
                    ("city", "original_city"),
                    ("state", "original_state"),
                )
            )
        ):
            raise ValueError(f"Coordinate export differs from accepted evidence for {identifier}.")
        paths = origins.get(record_key(item), set())
        if not paths:
            raise ValueError(f"Selected coordinate {identifier} has no matching candidate input.")
        for path in paths:
            origin_ids[path].add(identifier)
        source_counts[source] += 1
        method_ids[SOURCE_METHOD[source]].append(identifier)
        for record in [item, *item.get("corroborating_sources", [])]:
            retained_sources[record["source"]].add(identifier)
        if item["operational_status"] == "closed":
            excluded.append(identifier)
        else:
            routable_sites.add(int(item.get("site_id") or identifier))
            unknown_status += item["operational_status"] == "unknown"

    initial_ids = set(baseline.get("accepted_ids", []))
    if len(initial_ids) != len(baseline.get("accepted_ids", [])):
        raise ValueError("The baseline contains duplicate accepted IDs.")
    if baseline and baseline.get("accepted_count") != len(initial_ids):
        raise ValueError("Baseline accepted count does not match its station IDs.")
    if not initial_ids <= us_ids:
        raise ValueError("The baseline contains unknown US station IDs.")
    added = sorted(set(selected) - initial_ids) if baseline else []
    removed = sorted(initial_ids - set(selected))
    new_method_counts = Counter(
        SOURCE_METHOD[selected[identifier]["source"]] for identifier in added
    )
    counts = {
        "input_observations": len(rows),
        "non_us_observations": len(rows) - len(us_rows),
        "us_observations": len(us_rows),
        "us_station_ids": len(us_ids),
        "duplicate_us_observations": len(us_rows) - len(us_ids),
        "accepted_station_ids": len(selected),
        "unresolved_station_ids": len(pending),
        "coordinate_conflict_station_ids": len(disputed),
        "status_excluded_station_ids": len(excluded),
        "routable_station_ids": len(selected) - len(excluded),
        "routable_distinct_sites": len(routable_sites),
        "extra_routable_alias_ids": len(selected) - len(excluded) - len(routable_sites),
        "routable_ids_with_unknown_operational_status": unknown_status,
    }
    return {
        "counts": counts,
        "run": {
            "baseline_recorded_at": baseline.get("recorded_at"),
            "baseline_accepted_station_ids": len(initial_ids) if baseline else None,
            "new_accepted_ids": added,
            "removed_accepted_ids": removed,
            "net_new_accepted_count": len(added) - len(removed),
        },
        "methods": [
            {
                "method": key,
                "name": name,
                "description": description,
                "accepted_count": len(method_ids[key]),
                "new_accepted_count": new_method_counts[key],
                "opis_ids": sorted(method_ids[key]),
                "selected_source_counts": {
                    source: source_counts[source] for source in sources if source_counts[source]
                },
            }
            for key, (name, description, sources) in METHODS.items()
        ],
        "retained_coordinate_source_counts_nonadditive": {
            source: len(identifiers) for source, identifiers in sorted(retained_sources.items())
        },
        "selected_input_files_nonadditive": {
            name: len(identifiers) for name, identifiers in sorted(origin_ids.items())
        },
        "status_excluded_opis_ids": sorted(excluded),
    }


def census_summary(sources, anchor_csv):
    """Count search anchors separately; they do not themselves establish fuel premises."""
    with anchor_csv.open(encoding="utf-8-sig", newline="") as handle:
        initial = list(csv.DictReader(handle))
    batches = []
    for name in CENSUS_BATCHES:
        path = sources / name
        if not path.exists():
            continue
        records = json.loads(path.read_text(encoding="utf-8"))
        exact_ids = {
            int(item["opis_id"])
            for item in records
            if item.get("status", item.get("match_status")) == "Match"
            and item.get("match_type") == "Exact"
            and {"latitude", "longitude"} <= item.keys()
        }
        batches.append(
            {"file": name, "lookup_records": len(records), "exact_anchor_ids": len(exact_ids)}
        )
    return {
        "initial_exact_address_anchors": sum(
            item["source"] == "census_exact_address" for item in initial
        ),
        "later_batches_nonadditive": batches,
    }


STATUS_CATEGORIES = {
    "catscale_temporary": "CAT Scale temporary-closure label",
    "maps_temporary": "Maps temporary-closure label",
    "maps_permanent": "Maps permanent-closure label",
    "former_station": "Property advertised as a former gas station",
    "availability_conflict": "Conflicting operating and closure claims",
}


def status_summary(reviews, excluded_ids):
    """Require an explicit reviewed reason for every status exclusion."""
    records = indexed(reviews, "status reviews")
    if set(records) != set(excluded_ids):
        raise ValueError("Status reviews must cover exactly the status-excluded IDs.")
    if any(item["category"] not in STATUS_CATEGORIES for item in reviews):
        raise ValueError("Unknown status-exclusion category.")
    return [
        {
            "category": category,
            "description": description,
            "opis_ids": sorted(
                identifier for identifier, item in records.items() if item["category"] == category
            ),
            "count": sum(item["category"] == category for item in reviews),
        }
        for category, description in STATUS_CATEGORIES.items()
    ]


def scripted_review_count(accepted):
    """Count the retained evidence pattern emitted by loves_ta_conflicts.py."""
    return sum(
        item["source"] == "manually_reviewed_station_premises"
        and len(item.get("evidence", [])) >= 2
        and isinstance(item["evidence"][0], dict)
        and isinstance(item["evidence"][1], dict)
        and item["evidence"][0]
        .get("reason", "")
        .startswith("Official chain record matches station identity and street address:")
        and item["evidence"][1].get("reason", "").startswith("Independent named fuel premises (")
        for item in accepted
    )


def markdown(report):
    counts = report["counts"]
    percentage = 100 * counts["accepted_station_ids"] / counts["us_station_ids"]
    lines = [
        "# Station data",
        "",
        (
            f"**{counts['accepted_station_ids']:,} of {counts['us_station_ids']:,} US station IDs "
            f"have accepted coordinates ({percentage:.1f}%). "
            f"{counts['routable_distinct_sites']:,} distinct sites are eligible for planning.**"
        ),
        "",
        (
            "Prices come only from the supplied CSV. Station coordinates are prepared in advance; "
            "trip requests do not search station directories."
        ),
        "",
        "## Coverage",
        "",
        "| Measure | Count |",
        "| --- | ---: |",
    ]
    for label, key in (
        ("Input price observations", "input_observations"),
        ("Non-US observations excluded", "non_us_observations"),
        ("US observations", "us_observations"),
        ("Repeated US observations", "duplicate_us_observations"),
        ("Unique US station IDs", "us_station_ids"),
        ("Mapped station IDs", "accepted_station_ids"),
        ("Unresolved station IDs", "unresolved_station_ids"),
        ("Mapped IDs excluded by status", "status_excluded_station_ids"),
        ("Eligible station IDs", "routable_station_ids"),
        ("Extra IDs for duplicate physical sites", "extra_routable_alias_ids"),
        ("Distinct eligible sites", "routable_distinct_sites"),
    ):
        lines.append(f"| {label} | {counts[key]:,} |")
    lines.extend(
        [
            "",
            (
                "Repeated observations use their arithmetic mean. Reviewed duplicate properties "
                "combine all their price observations. "
                f"{counts['routable_ids_with_unknown_operational_status']:,} of "
                f"{counts['routable_station_ids']:,} eligible IDs have "
                "opening status recorded as unknown."
            ),
            "",
            "## Coordinate methods",
            "",
            (
                "Each mapped ID appears once, by its selected coordinate source. These counts "
                "do not separate direct downloads from extracts or scripted work from browser review."
            ),
            "",
            "| Selected source/method | IDs | How the point was obtained |",
            "| --- | ---: | --- |",
        ]
    )
    descriptions = {
        "operator": "Extract published map coordinates; match store identity and locality.",
        "operator_snapshot": "Match records in downloaded operator-directory extracts.",
        "historical_identity": "Join old store identities to current points through the same street address.",
        "manual_premises": "Resolve identities and conflicting points with scripted checks and individual reviews.",
        "reviewed_published": "Retain a published point after checking station identity and property.",
        "mapped_address": "Match a published street and station identity to a mapped property.",
        "fuel_network": "Match Commercial Fueling Network (CFN) or Fuelman locator records.",
        "osm_identity": "Match distinctive station names or store numbers to OpenStreetMap fuel features.",
        "federal_gps": "Match streets to US Alternative Fuels Data Center (AFDC) station GPS records.",
        "scale": "Match the host truck stop; use its on-property CAT Scale point.",
        "archive": "Match original numbered stations to older operator-network records.",
    }
    for method in report["methods"]:
        lines.append(
            f"| {method['name']} | {method['accepted_count']:,} | "
            f"{descriptions[method['method']]} |"
        )
    lines.extend(
        [
            f"| **Total** | **{counts['accepted_station_ids']:,}** | |",
            "",
            (
                "Some operator points arrived through AllThePlaces extracts, which collect public "
                "store locators into GeoJSON. Extracts and their original feeds are not independent "
                "sources. Historical joins handle renamed or renumbered stations."
            ),
            "",
            (
                f"At least {report['scripted_review_minimum']:,} records in the premises-review group "
                "carry evidence from automated conflict checks. Other records include browser listing, "
                "Street View or aerial review. There is no reliable total separating those activities. "
                "Google Maps website review was used; bulk Google Places API search was not."
            ),
            "",
            "## Status exclusions",
            "",
            "| Recorded reason | IDs |",
            "| --- | ---: |",
        ]
    )
    for group in report["status_exclusion_groups"]:
        lines.append(f"| {group['description']} | {group['count']:,} |")
    lines.extend(
        [
            f"| **Total** | **{counts['status_excluded_station_ids']:,}** |",
            "",
            (
                "These are exclusions, not all confirmed permanent closures. A CAT Scale label may "
                "refer to the scale rather than every fueling service. The conflicting case is "
                "Broadway/former Flying J in Pasco, WA. The API currently groups these under "
                "`known_closed_stations`. See [status reviews](coordinate-sources/status-exclusion-reviews.json) "
                "for station IDs and evidence."
            ),
        ]
    )
    search = report.get("tomtom_search", {})
    reasons = search.get("remaining_context_reasons", {})
    if reasons:
        if sum(reasons.values()) != counts["unresolved_station_ids"]:
            raise ValueError("Unresolved reasons must match the report's unresolved total.")
        labels = {
            "no_independent_branch": "No exact independent branch identity",
            "no_actual_mapped_fuel_within_100m": "No compatible mapped fuel feature within 100 metres",
            "generic_brand_without_original_street": "Generic brand without a street address",
            "independent_branch_and_mapped_fuel": "Candidate branch/property still needs confirmation",
            "held_identity": "Recorded identity or equipment concern",
            "multiple_mapped_fuel_properties": "Multiple nearby mapped fuel properties",
            "city_only_name_without_original_street": "City-only identity",
            "ambiguous_independent_streets": "Conflicting street addresses",
            "independent_coordinate_conflict": "Conflicting independent coordinates",
        }
        lines.extend(
            [
                "",
                "## Unresolved stations",
                "",
                "| Recorded reason | IDs |",
                "| --- | ---: |",
            ]
        )
        for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"| {labels.get(reason, reason.replace('_', ' '))} | {count:,} |")
        lines.extend(
            [
                f"| **Total** | **{counts['unresolved_station_ids']:,}** |",
                "",
                (
                    "Reasons are mutually exclusive. Unresolved means the applied checks did not "
                    "establish a reliable station match; it does not mean closed or impossible to locate. "
                    "Missing mapped fuel can reflect incomplete map coverage."
                ),
            ]
        )
    lines.extend(
        [
            "",
            "## Supporting searches",
            "",
            (
                f"The original Census batch returned {report['census']['initial_exact_address_anchors']:,} "
                "exact address matches. These were search anchors, not accepted station premises. "
                "City centres and highway exits were also search anchors, not fuel-stop coordinates."
            ),
        ]
    )
    if search:
        total = search["pilot_http_requests"] + search["search_request_count"]
        lines.extend(
            [
                "",
                (
                    f"TomTom made {total:,} HTTP requests: {search['pilot_http_requests']:,} pilot and "
                    f"{search['search_request_count']:,} bulk requests covering "
                    f"{search['searched_station_count']:,} IDs. It assisted "
                    f"{len(search['independently_accepted_ids']):,} accepted matches; a separate audit "
                    f"accepted {len(search.get('independent_audit_only_accepted_ids', [])):,} more. "
                    "All use independently sourced final points and are already counted above. "
                    "TomTom response bodies were not retained."
                ),
            ]
        )
    lines.extend(
        [
            "",
            "## Files and limits",
            "",
            (
                "[verified-station-coordinates.csv](verified-station-coordinates.csv) is the only file "
                "in `data/` required for the application import. Git retains eight final data files: "
                "the mapping, two reports, accepted and unresolved station records, alias and status "
                "reviews, and one live validation record. Research intermediates, duplicate summaries "
                "and historical debugging records remain local and are ignored. "
                "Full research rebuilds require those local inputs."
            ),
            "",
            (
                "[Detailed counts](station-coordinate-report.json) and [selected evidence]"
                "(coordinate-sources/merged.json) preserve station IDs, sources and reviews."
            ),
            "",
            (
                "Accepted points can locate a station building, canopy, scale or premises; they do "
                "not guarantee entrance access, current operation or fuel availability. Missing "
                "stations can remove a cheaper option or prevent a feasible plan. Optimization "
                "therefore covers the eligible subset, not every real station or road route."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def validate_search_summary(search, accepted_ids):
    confirmed_ids = search["independently_accepted_ids"]
    if len(confirmed_ids) != len(set(confirmed_ids)) or not set(confirmed_ids) <= accepted_ids:
        raise ValueError("TomTom independent acceptances must be unique final accepted IDs.")
    audit_ids = search.get("independent_audit_only_accepted_ids", [])
    if (
        len(audit_ids) != len(set(audit_ids))
        or not set(audit_ids) <= accepted_ids
        or set(audit_ids) & set(confirmed_ids)
    ):
        raise ValueError(
            "Independent-audit-only acceptances must be unique and disjoint accepted IDs."
        )
    if search["response_retained"] is not False:
        raise ValueError("The TomTom search report requires unretained provider responses.")
    remaining_reasons = search.get("remaining_context_reasons")
    if (
        remaining_reasons is not None
        and sum(remaining_reasons.values()) != search["remaining_station_count"]
    ):
        raise ValueError("Remaining exclusion reason counts must sum to remaining station IDs.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=ROOT / "data/coordinate-run-baseline.json")
    args = parser.parse_args()
    paths = {
        "prices": ROOT / "fuel-prices-for-be-assessment.csv",
        "accepted": SOURCES / "merged.json",
        "unresolved": SOURCES / "unresolved.json",
        "conflicts": SOURCES / "conflicts.json",
        "coordinate_export": ROOT / "data/verified-station-coordinates.csv",
    }
    records = {
        key: json.loads(path.read_text(encoding="utf-8"))
        for key, path in paths.items()
        if path.suffix == ".json"
    }
    boundaries = json.loads((ROOT / "trips/data/us-states.geojson").read_text(encoding="utf-8"))
    states = {feature["properties"]["STUSAB"] for feature in boundaries["features"]}
    origins = defaultdict(set)
    for path in sorted(
        [*SOURCES.glob("*-candidates.json"), *SOURCES.glob("*-conflict-reviews.json")]
    ):
        for item in json.loads(path.read_text(encoding="utf-8")):
            origins[record_key(item)].add(path.name)
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    report = build_report(
        read_csv(paths["prices"]),
        states,
        records["accepted"],
        records["unresolved"],
        records["conflicts"],
        read_csv(paths["coordinate_export"]),
        origins,
        baseline,
    )
    status_path = SOURCES / "status-exclusion-reviews.json"
    report["status_exclusion_groups"] = status_summary(
        json.loads(status_path.read_text(encoding="utf-8")), report["status_excluded_opis_ids"]
    )
    report["scripted_review_minimum"] = scripted_review_count(records["accepted"])
    paths["status_reviews"] = status_path
    report["generated_at"] = datetime.now(UTC).isoformat()
    report["artifact_sha256"] = {
        str(path.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in [*paths.values(), args.baseline]
    }
    report["census"] = census_summary(SOURCES, ROOT / "data/station-coordinates.csv")
    search_path = ROOT / "data/tomtom-search-summary.json"
    if search_path.exists():
        search = json.loads(search_path.read_text(encoding="utf-8"))
        accepted_ids = {item["opis_id"] for item in records["accepted"]}
        validate_search_summary(search, accepted_ids)
        report["tomtom_search"] = search
        report["artifact_sha256"][str(search_path.relative_to(ROOT)).replace("\\", "/")] = (
            hashlib.sha256(search_path.read_bytes()).hexdigest()
        )
        audit_path = SOURCES / "tomtom-independent-audit.json"
        if audit_path.exists():
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            report["independent_audit"] = {
                key: audit[key] for key in ("candidate_count", "primary_identity_source_counts")
            }
            report["artifact_sha256"][str(audit_path.relative_to(ROOT)).replace("\\", "/")] = (
                hashlib.sha256(audit_path.read_bytes()).hexdigest()
            )
    (ROOT / "data/station-coordinate-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (ROOT / "data/station-coordinate-report.md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps(report["counts"], indent=2))


if __name__ == "__main__":
    main()
