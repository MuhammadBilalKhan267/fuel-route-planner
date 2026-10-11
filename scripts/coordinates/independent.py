"""Acquire official independent-network station coordinates and match CSV identities.

Default execution is offline. --fetch acquires only missing source caches using
public, unauthenticated GETs. HTTP errors stop the provider without retries.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from catscale import brand_names, location_matches, matching_name, road_details
from matching import station_name_keys
from pilot import US_STATES, normalized

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "data" / "coordinate-sources"
AMBEST_URL = "https://am-best.com/ambest_new/public/api/get-user-locations-and-fuel-price"
RANGER_URL = "https://www.roadrangerusa.com/locations-amenities/find-a-road-ranger"
SAPP_URL = "https://www.sappbros.net/travel-centers/locations/"


def get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8-sig")


def plain(text: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())


def record(provider: str, row: dict) -> dict:
    latitude, longitude = float(row["latitude"]), float(row["longitude"])
    if not (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    ):
        raise ValueError(f"Invalid {provider} coordinate: {row['external_id']}")
    return {
        **row,
        "latitude": latitude,
        "longitude": longitude,
        "source": f"{provider}_official_location",
        "availability": "closed" if "CLOSED" in row["name"].upper() else "unknown",
    }


def store_number(name: str) -> str | None:
    pattern = r"CIRCLE\s*K\s*#?\s*(\d+)" if "circle_k" in brand_names(name) else r"#\s*(\d+)"
    match = re.search(pattern, name, re.IGNORECASE)
    return match.group(1) if match else None


def identity(left: str, right: str, city: str = "") -> list[str]:
    left_brands, right_brands = brand_names(left), brand_names(right)
    left_number, right_number = store_number(left), store_number(right)
    same_number = left_number and right_number and int(left_number) == int(right_number)
    suffix_number = (
        left_brands & right_brands == {"circle_k"}
        and left_number
        and right_number
        and min(len(left_number), len(right_number)) <= 4
        and int(left_number[-4:]) == int(right_number[-4:])
    )
    if left_number and right_number and not (same_number or suffix_number):
        return []
    left_keys, right_keys = station_name_keys(left, city), station_name_keys(right, city)
    city_suffix_match = (
        bool(city)
        and (len(left_keys) > 1 or len(right_keys) > 1)
        and any(len(key) >= 7 for key in left_keys & right_keys)
        and not matching_name(left, right)
    )
    if (
        not matching_name(left, right)
        and not (suffix_number and left_brands & right_brands)
        and not city_suffix_match
    ):
        return []
    return (
        ["same_store_number"]
        if same_number
        else ["same_circle_k_store_suffix"]
        if suffix_number
        else ["same_station_name_with_matching_city_suffix"]
        if city_suffix_match
        else ["same_brand_or_station_name"]
    )


def extended_location_matches(row: dict, source: dict) -> list[str]:
    evidence = location_matches(row, source)
    if evidence:
        return evidence
    left_interstates, left_exits = road_details(row["Address"])
    right_interstates, right_exits = road_details(source["address"])
    mile_markers = set(re.findall(r"\bMM\s*(\d+)", source["address"].upper()))
    if left_interstates & right_interstates and mile_markers & {
        re.sub(r"[A-D]$", "", value) for value in left_exits
    }:
        return ["same_interstate_and_mile_marker_matches_exit_number"]
    if left_interstates & right_interstates and left_exits and right_exits:
        if {re.sub(r"[A-D]$", "", value) for value in left_exits} & {
            re.sub(r"[A-D]$", "", value) for value in right_exits
        }:
            return ["same_interstate_and_exit_number"]
        return []

    def highways(text: str) -> set[tuple[str, str]]:
        patterns = (
            ("US", r"\bUS\s*[- ]*\s*(?:HWY\s*|HIGHWAY\s*)?(\d+(?:[/&]\d+)*)"),
            ("STATE", r"\b(?:SR|STATE\s*(?:HWY|HIGHWAY)|RT|RTE|ROUTE)\s*[- ]*\s*(\d+(?:[/&]\d+)*)"),
            ("GENERIC", r"\b(?:HWY|HIGHWAY)\s*[- ]*\s*(\d+(?:[/&]\d+)*)"),
        )
        result = set()
        typed_spans = []
        for kind, pattern in patterns:
            for match in re.finditer(pattern, text.upper()):
                if kind == "GENERIC" and any(
                    start <= match.start() < end for start, end in typed_spans
                ):
                    continue
                if kind != "GENERIC":
                    typed_spans.append(match.span())
                result.update((kind, number) for number in re.findall(r"\d+", match.group(1)))
        return result

    left, right = highways(row["Address"]), highways(source["address"])
    if left & right or any(
        number == other_number and "GENERIC" in {kind, other_kind}
        for kind, number in left
        for other_kind, other_number in right
    ):
        return ["same_us_or_state_highway"]
    return []


def ambest() -> list[dict]:
    sources = []
    for row in json.loads(get(AMBEST_URL))["data"]:
        if row["state"] not in US_STATES or not row["lat"] or not row["long"]:
            continue
        sources.append(
            record(
                "ambest",
                {
                    "external_id": str(row["id"]),
                    "internal_id": row["internal_id"],
                    "name": row["name"],
                    "address": row["directory_address"] or "",
                    "city": row["city"],
                    "state": row["state"],
                    "postal_code": row["zip"] or "",
                    "latitude": row["lat"],
                    "longitude": row["long"],
                    "source_url": AMBEST_URL,
                    "station_url": row["website"],
                    "directory_category": row["star_color"],
                },
            )
        )
    return sources


def road_ranger() -> list[dict]:
    map_cache = OUTPUT / "independent-road-ranger-map-records.json"
    code_cache = OUTPUT / "independent-road-ranger-opis-codes.json"
    if map_cache.exists() and code_cache.exists():
        codes = {
            normalized(row["address"]): row
            for row in json.loads(code_cache.read_text(encoding="utf-8"))
        }
        previous = {
            normalized(row.get("map_label", row["external_id"])): row
            for row in json.loads(
                (OUTPUT / "independent-road-ranger-stations.json").read_text(encoding="utf-8")
            )
        }
        rows = []
        for row in json.loads(map_cache.read_text(encoding="utf-8")):
            code = codes[normalized(row["map_label"])]
            old = previous.get(normalized(row["map_label"]), {})
            rows.append(
                record(
                    "road_ranger",
                    {
                        **row,
                        "external_id": str(code["store_number"]),
                        "name": f"Road Ranger #{code['store_number']}",
                        "city": old.get("city", ""),
                        "state": old.get("state", ""),
                        "postal_code": old.get("postal_code", ""),
                        "published_opis_id": code["opis_id"],
                        "billing_address": code["address"],
                        "billing_source_url": code["source_url"],
                        "station_url": RANGER_URL,
                    },
                )
            )
        return rows
    content = get(RANGER_URL)
    headings = [plain(value) for value in re.findall(r"<h4[^>]*>(.*?)</h4>", content, re.DOTALL)]
    sources = []
    blocks = re.split(r'<div\s+[^>]*class="geolocation-location', content)[1:]
    for block in blocks:
        coordinate = re.search(r'data-lat="([^"]+)" data-lng="([^"]+)"', block)
        label = re.search(r'<a class="location-pin"[^>]*>(.*?)</a>', block, re.DOTALL)
        if not coordinate or not label:
            continue
        short_address = plain(label.group(1))
        address = next(
            (title for title in headings if title.startswith(short_address)), short_address
        )
        locality = re.search(r",\s*(.*?)\s+([A-Z]{2})\b", short_address)
        if not locality or locality.group(2) not in US_STATES:
            continue
        city, state = locality.groups()
        sources.append(
            record(
                "road_ranger",
                {
                    "external_id": normalized(short_address),
                    "name": "Road Ranger",
                    "address": address,
                    "city": city.strip(", "),
                    "state": state,
                    "postal_code": "",
                    "latitude": coordinate.group(1),
                    "longitude": coordinate.group(2),
                    "source_url": RANGER_URL,
                    "station_url": RANGER_URL,
                },
            )
        )
    return sources


def sapp_bros() -> list[dict]:
    content = get(SAPP_URL)
    config = re.search(r"var ASL_REMOTE\s*=\s*(\{.*?\});", content, re.DOTALL)
    if not config:
        raise ValueError("Sapp Bros public locator request configuration missing")
    remote = json.loads(config.group(1))
    query = urllib.parse.urlencode(
        {"action": "asl_load_stores", "nonce": remote["nonce"], "load_all": "1", "layout": "0"}
    )
    time.sleep(1)
    rows = json.loads(get(f"{remote['ajax_url']}?{query}"))
    return [
        record(
            "sapp_bros",
            {
                "external_id": row["id"],
                "name": f"Sapp Bros {row['title']}",
                "address": row["street"],
                "city": row["city"],
                "state": row["state"],
                "postal_code": row["postal_code"],
                "latitude": row["lat"],
                "longitude": row["lng"],
                "source_url": SAPP_URL,
                "station_url": row["website"],
                "directory_categories": row["categories"],
            },
        )
        for row in rows
        if row["state"] in US_STATES and "20" in row["categories"].split(",")
    ]


def roadys() -> list[dict]:
    path = OUTPUT / "independent-roadys-alltheplaces.geojson"
    if not path.exists():
        return []
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    sources = []
    for feature in snapshot["features"]:
        row = feature["properties"]
        if row.get("addr:state") not in US_STATES:
            continue
        longitude, latitude = feature["geometry"]["coordinates"]
        source = record(
            "roadys",
            {
                "external_id": row["ref"],
                "name": row["name"],
                "address": row.get("addr:street_address", ""),
                "city": row.get("addr:city", ""),
                "state": row["addr:state"],
                "postal_code": row.get("addr:postcode", ""),
                "latitude": latitude,
                "longitude": longitude,
                "source_url": row["@source_uri"],
                "snapshot_url": snapshot["source_url"],
                "snapshot_run": snapshot["source_run"],
            },
        )
        source["source"] = "roadys_official_locator_via_alltheplaces"
        sources.append(source)
    return sources


def match(sources: list[dict], csv_path: Path) -> tuple[list[dict], list[dict]]:
    by_city = defaultdict(list)
    for source in sources:
        by_city[(source["state"], normalized(source["city"]))].append(source)
    stations = defaultdict(list)
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["State"] in US_STATES:
                stations[int(row["OPIS Truckstop ID"])].append(row)
    # The billing PDF provides a primary-source join between physical addresses,
    # store numbers and the CSV's OPIS identifier. No location inference is needed.
    ranger_by_opis = {
        row["published_opis_id"]: row for row in sources if row.get("published_opis_id") is not None
    }
    catscale_path = OUTPUT / "catscale-candidates.json"
    corroborated = (
        {row["opis_id"]: row for row in json.loads(catscale_path.read_text(encoding="utf-8"))}
        if catscale_path.exists()
        else {}
    )
    fuelman_path = OUTPUT / "fuelman-candidates.json"
    fuelman = (
        {row["opis_id"]: row for row in json.loads(fuelman_path.read_text(encoding="utf-8"))}
        if fuelman_path.exists()
        else {}
    )

    def nearby(source: dict, existing: dict) -> bool:
        lat1, lat2 = math.radians(source["latitude"]), math.radians(existing["latitude"])
        delta_lat = lat2 - lat1
        delta_lon = math.radians(existing["longitude"] - source["longitude"])
        distance = 12742000 * math.asin(
            math.sqrt(
                math.sin(delta_lat / 2) ** 2
                + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
            )
        )
        return distance <= 300

    accepted, unresolved = [], []
    for opis_id, variants in sorted(stations.items()):
        matches = {}
        for row in variants:
            local_sources = list(by_city[(row["State"], normalized(row["City"]))])
            if direct_source := ranger_by_opis.get(opis_id):
                local_sources.append(direct_source)
            # CSV cities are used only to recognize text explicitly printed in
            # the official address label, including labels without a comma.
            local_sources.extend(
                source
                for source in sources
                if source["source"] == "road_ranger_official_location"
                and not source["city"]
                and normalized(row["City"]) in normalized(source["address"])
            )
            for source in local_sources:
                name_evidence = identity(row["Truckstop Name"], source["name"], row["City"])
                if not name_evidence:
                    continue
                evidence = extended_location_matches(row, source)
                if source.get("published_opis_id") == opis_id:
                    evidence.extend(
                        ("same_published_opis_id", "official_billing_address_matches_map_label")
                    )
                if (
                    not evidence
                    and source["source"] == "sapp_bros_official_location"
                    and opis_id in corroborated
                    and nearby(source, corroborated[opis_id])
                ):
                    evidence.append("within_300m_of_name_city_state_highway_verified_cat_scale")
                if source["source"] == "roadys_official_locator_via_alltheplaces":
                    if opis_id in corroborated and nearby(source, corroborated[opis_id]):
                        evidence.append("within_300m_of_name_city_state_highway_verified_cat_scale")
                    if opis_id in fuelman:
                        street = fuelman[opis_id]["matched_address"].split(",")[0]
                        if normalized(street) == normalized(source["address"]):
                            evidence.append(
                                "same_actual_street_address_in_independent_fuelman_record"
                            )
                if evidence or "same_store_number" in name_evidence:
                    matches[(source["source"], source["external_id"])] = (
                        source,
                        [*name_evidence, *evidence],
                    )
        # Several independent directories can corroborate the same physical station.
        provider_matches = defaultdict(list)
        for source, evidence in matches.values():
            provider_matches[source["source"]].append((source, evidence))
        if matches and all(len(values) == 1 for values in provider_matches.values()):
            for source, evidence in matches.values():
                accepted.append(
                    {
                        "opis_id": opis_id,
                        "latitude": source["latitude"],
                        "longitude": source["longitude"],
                        "source": source["source"],
                        "source_url": source["source_url"],
                        "external_id": source["external_id"],
                        "matched_name": source["name"],
                        "matched_address": ", ".join(
                            (
                                source["address"],
                                source["city"],
                                source["state"],
                                source["postal_code"],
                            )
                        ),
                        "evidence": ["same_city", "same_state", *evidence]
                        if source["city"]
                        else [*evidence, "city_explicit_in_official_address_or_published_opis_id"],
                        "availability": source["availability"],
                        "original_names": sorted({row["Truckstop Name"] for row in variants}),
                        "original_address": variants[0]["Address"],
                        "original_city": variants[0]["City"],
                        "original_state": variants[0]["State"],
                        **(
                            {"billing_source_url": source["billing_source_url"]}
                            if source.get("billing_source_url")
                            else {}
                        ),
                        **(
                            {
                                "snapshot_url": source["snapshot_url"],
                                "snapshot_run": source["snapshot_run"],
                            }
                            if source.get("snapshot_url")
                            else {}
                        ),
                    }
                )
        elif by_city[(variants[0]["State"], normalized(variants[0]["City"]))]:
            unresolved.append(
                {
                    "opis_id": opis_id,
                    "reason": "ambiguous_identity" if matches else "no_corroborated_identity",
                    "names": sorted({row["Truckstop Name"] for row in variants}),
                    "address": variants[0]["Address"],
                    "city": variants[0]["City"],
                    "state": variants[0]["State"],
                    "candidates": list(matches),
                }
            )
    used = defaultdict(list)
    for candidate in accepted:
        used[(candidate["source"], candidate["external_id"])].append(candidate)
    collisions = {row["opis_id"] for values in used.values() if len(values) > 1 for row in values}
    unresolved.extend(
        {"opis_id": opis_id, "reason": "same_source_matched_multiple_opis_ids"}
        for opis_id in sorted(collisions)
    )
    return [row for row in accepted if row["opis_id"] not in collisions], unresolved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--csv", type=Path, default=ROOT / "fuel-prices-for-be-assessment.csv")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    sources, failures = [], []
    for provider, download in (
        ("ambest", ambest),
        ("road-ranger", road_ranger),
        ("sapp-bros", sapp_bros),
    ):
        path = OUTPUT / f"independent-{provider}-stations.json"
        if (
            provider == "road-ranger"
            and (OUTPUT / "independent-road-ranger-opis-codes.json").exists()
        ):
            rows = road_ranger()
            path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        elif path.exists():
            rows = json.loads(path.read_text(encoding="utf-8"))
        elif args.fetch:
            try:
                rows = download()
            except (urllib.error.URLError, ValueError, KeyError) as exc:
                failures.append({"provider": provider, "reason": str(exc), "retried": False})
                continue
            path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        else:
            raise SystemExit(f"Missing {provider} source cache; use --fetch.")
        sources.extend(rows)
    sources.extend(roadys())
    details_path = OUTPUT / "independent-sapp-bros-details.json"
    if details_path.exists():
        details = {
            row["external_id"]: row
            for row in json.loads(details_path.read_text(encoding="utf-8"))
            if row.get("success") and row.get("highway_address")
        }
        for source in sources:
            if (
                source["source"] == "sapp_bros_official_location"
                and source["external_id"] in details
            ):
                detail = details[source["external_id"]]
                source["address"] = f"{source['address']} | {detail['highway_address']}"
                source["source_url"] = detail["source_url"]
    accepted, unresolved = match(sources, args.csv)
    for name, rows in (
        ("candidates", accepted),
        ("unresolved", unresolved),
        ("acquisition-report", failures),
    ):
        (OUTPUT / f"independent-{name}.json").write_text(
            json.dumps(rows, indent=2) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "source_records": len(sources),
                "candidates": len(accepted),
                "unique_opis_ids": len({row["opis_id"] for row in accepted}),
                "failures": failures,
            }
        )
    )


if __name__ == "__main__":
    main()
