"""Fetch Pilot's public locator data and match station identities from the price CSV.

Run with --fetch once; subsequent runs use the cached responses without network I/O.
Coordinates come from the official locator, never its cityCoordinate field.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import re
import time
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "data" / "coordinate-sources"
BASE_URL = "https://locations.pilotflyingj.com/search"
US_STATES = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "DC",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
}
FAMILY = re.compile(r"PILOT|FLYING\s*J|ONE\s*9|MR\.?\s*FUEL|STAMART|PRIDE", re.IGNORECASE)
PROFILE_FIELDS = {
    "address",
    "c_externalStoreNumber",
    "c_siteID",
    "c_pagesName",
    "c_pagesURL",
    "c_siteBrand",
    "c_interstate",
    "c_liveOnPages",
    "name",
    "landingPageUrl",
    "yextDisplayCoordinate",
    "displayCoordinate",
    "yextRoutableCoordinate",
    "routableCoordinate",
}


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).upper()
    text = re.sub(r"\bSAINT\b", "ST", text)
    text = re.sub(r"\bFORT\b", "FT", text)
    return re.sub(r"[^A-Z0-9]", "", text)


def highway_exits(text: str) -> tuple[set[str], set[str]]:
    highways = set(re.findall(r"\bI\s*[- ]?\s*(\d+)\b", text.upper()))
    exits = set(re.findall(r"\bEXIT\s*(\d+[A-Z]?)\b", text.upper()))
    return highways, exits


def fetch_pages(fetch: bool) -> list[dict]:
    records = []
    offset = 0
    total = None
    previous_request = 0.0
    OUTPUT.mkdir(parents=True, exist_ok=True)
    while total is None or offset < total:
        path = OUTPUT / f"pilot-page-{offset:04d}.json.gz"
        if path.exists():
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                page = json.load(stream)
        else:
            if not fetch:
                raise SystemExit(f"Missing cached page {path.name}; use --fetch to acquire it.")
            time.sleep(max(0.0, 1.0 - (time.monotonic() - previous_request)))
            url = f"{BASE_URL}?per=50&offset={offset}"
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "StationCoordinateAssessment/1.0",
                },
            )
            previous_request = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                raise SystemExit(
                    f"Pilot returned HTTP {exc.code}; stopped without retries."
                ) from exc
            page = json.loads(raw)
            # Cache station fields only; provider configuration and contacts are unnecessary.
            page = {
                "source_url": url,
                "response": {
                    "count": page["response"]["count"],
                    "entities": [
                        {
                            "profile": {
                                key: value
                                for key, value in entity["profile"].items()
                                if key in PROFILE_FIELDS
                            }
                        }
                        for entity in page["response"]["entities"]
                    ],
                },
            }
            with gzip.open(path, "wt", encoding="utf-8") as stream:
                json.dump(page, stream)
            print(f"Cached offset {offset}: {len(raw):,} bytes", flush=True)
        response = page["response"]
        total = int(response["count"])
        entities = response["entities"]
        if not entities:
            raise SystemExit(f"Empty page at {offset} before expected total {total}.")
        records.extend(entity["profile"] for entity in entities)
        offset += len(entities)
    return records


def source_record(profile: dict) -> dict | None:
    address = profile.get("address", {})
    if (
        address.get("countryCode") != "US"
        or address.get("region") not in US_STATES
        or not profile.get("c_liveOnPages")
    ):
        return None
    # Official map display points identify station premises, not municipal centroids.
    coordinate = profile.get("yextDisplayCoordinate") or profile.get("displayCoordinate")
    if not coordinate:
        return None
    latitude, longitude = float(coordinate["lat"]), float(coordinate["long"])
    if not (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    ):
        raise ValueError("Official Pilot locator supplied an invalid station coordinate.")
    return {
        "external_id": str(profile.get("c_externalStoreNumber") or profile.get("c_siteID")),
        "name": profile.get("c_pagesName") or profile.get("name"),
        "brand": profile.get("c_siteBrand", ""),
        "address": address.get("line1", ""),
        "city": address.get("city", ""),
        "state": address.get("region", ""),
        "postal_code": address.get("postalCode", ""),
        "interstate": profile.get("c_interstate", ""),
        "latitude": latitude,
        "longitude": longitude,
        "source_url": profile.get("c_pagesURL") or profile.get("landingPageUrl"),
        "coordinate_field": "yextDisplayCoordinate"
        if profile.get("yextDisplayCoordinate")
        else "displayCoordinate",
        "routable_coordinate": profile.get("yextRoutableCoordinate")
        or profile.get("routableCoordinate"),
    }


def match_stations(sources: list[dict], csv_path: Path) -> tuple[list[dict], list[dict]]:
    by_id = defaultdict(list)
    for source in sources:
        by_id[source["external_id"]].append(source)
    stations = defaultdict(list)
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["State"] in US_STATES and FAMILY.search(row["Truckstop Name"]):
                stations[int(row["OPIS Truckstop ID"])].append(row)
    accepted, unresolved = [], []
    for opis_id, variants in sorted(stations.items()):
        numbers = {
            str(int(match.group(1) or match.group(2)))
            for row in variants
            for match in re.finditer(
                r"#\s*(\d+)|\bSTAMART\s+(\d+)\b", row["Truckstop Name"], re.IGNORECASE
            )
        }
        candidates = []
        if numbers:
            possible = [source for number in numbers for source in by_id[number]]
        else:
            possible = []
            for row in variants:
                name = normalized(row["Truckstop Name"])
                for source in sources:
                    source_brand = normalized(source["brand"])
                    if ("MRFUEL" in name and "MRFUEL" in source_brand) or (
                        "EXPRESSFUEL" in name and "XPRESS" in source_brand
                    ):
                        possible.append(source)
        for source in possible:
            for row in variants:
                if row["State"] != source["state"]:
                    continue
                city_match = normalized(row["City"]) == normalized(source["city"])
                input_highways, input_exits = highway_exits(row["Address"])
                source_highways, source_exits = highway_exits(source["interstate"])
                road_match = bool(input_highways & source_highways) and bool(
                    input_exits & source_exits
                )
                if (numbers and (city_match or road_match)) or (
                    not numbers and city_match and road_match
                ):
                    evidence = [
                        "official_store_number" if numbers else "official_station_brand",
                        "same_state",
                    ]
                    if city_match:
                        evidence.append("same_city")
                    if road_match:
                        evidence.append("same_interstate_and_exit")
                    candidates.append((source, evidence))
        unique = {source["external_id"]: (source, evidence) for source, evidence in candidates}
        if len(unique) == 1:
            source, evidence = next(iter(unique.values()))
            accepted.append(
                {
                    "opis_id": opis_id,
                    "latitude": source["latitude"],
                    "longitude": source["longitude"],
                    "source": "pilot_official_locator",
                    "source_url": source["source_url"],
                    "external_id": source["external_id"],
                    "matched_name": source["name"],
                    "matched_address": ", ".join(
                        (source["address"], source["city"], source["state"], source["postal_code"])
                    ),
                    "evidence": evidence,
                    "original_names": sorted({row["Truckstop Name"] for row in variants}),
                    "original_address": variants[0]["Address"],
                    "original_city": variants[0]["City"],
                    "original_state": variants[0]["State"],
                }
            )
        else:
            unresolved.append(
                {
                    "opis_id": opis_id,
                    "rows": variants,
                    "store_numbers": sorted(numbers),
                    "reason": "no_corroborated_identity" if not unique else "ambiguous_identity",
                    "same_number_sources": [
                        source for number in numbers for source in by_id[number]
                    ],
                }
            )
    return accepted, unresolved


def match_archive(archive: dict, csv_path: Path) -> list[dict]:
    """Corroborate archived Speedway network IDs with store number, city and state.

    The archived five-digit Speedway network IDs prefix the four-digit store ID
    with 8. Keep the network ID as the external identifier and retain the source
    year; an archived listing does not establish current availability.
    """
    sources = defaultdict(list)
    for source in archive["stations"]:
        network_id = source["external_id"]
        if source["name"] == "Speedway" and re.fullmatch(r"8\d{4}", network_id):
            sources[(str(int(network_id[1:])), source["state"], normalized(source["city"]))].append(
                source
            )
    accepted = {}
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if "SPEEDWAY" not in row["Truckstop Name"].upper():
                continue
            for number in re.findall(r"#\s*(\d+)", row["Truckstop Name"]):
                candidates = sources[(str(int(number)), row["State"], normalized(row["City"]))]
                if len(candidates) != 1:
                    continue
                source = candidates[0]
                opis_id = int(row["OPIS Truckstop ID"])
                accepted[opis_id] = {
                    "opis_id": opis_id,
                    "latitude": source["latitude"],
                    "longitude": source["longitude"],
                    "source": "pilot_official_archived_network_list",
                    "source_url": archive["source_url"],
                    "external_id": source["external_id"],
                    "matched_name": f"Speedway #{number}",
                    "matched_address": ", ".join(
                        (source["address"], source["city"], source["state"], source["postal_code"])
                    ),
                    "evidence": [
                        "same_station_brand",
                        "archived_network_id_matches_store_number",
                        "same_city",
                        "same_state",
                    ],
                    "source_year": archive["source_year"],
                    "availability": "unknown",
                    "original_names": [row["Truckstop Name"]],
                    "original_address": row["Address"],
                    "original_city": row["City"].strip(),
                    "original_state": row["State"],
                }
    return sorted(accepted.values(), key=lambda row: row["opis_id"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fetch", action="store_true", help="Fetch missing official pages at <=1/sec"
    )
    parser.add_argument("--csv", type=Path, default=ROOT / "fuel-prices-for-be-assessment.csv")
    args = parser.parse_args()
    profiles = fetch_pages(args.fetch)
    sources = [record for profile in profiles if (record := source_record(profile))]
    accepted, unresolved = match_stations(sources, args.csv)
    archive_path = OUTPUT / "pilot-one9-archive.json"
    archived = (
        match_archive(json.loads(archive_path.read_text(encoding="utf-8")), args.csv)
        if archive_path.exists()
        else []
    )
    for name, records in (
        ("pilot-stations.json", sources),
        ("pilot-candidates.json", accepted),
        ("pilot-unresolved.json", unresolved),
        ("pilot-archive-candidates.json", archived),
    ):
        (OUTPUT / name).write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "official_us_stations": len(sources),
                "accepted": len(accepted),
                "unresolved": len(unresolved),
                "archived_network_candidates": len(archived),
            }
        )
    )


if __name__ == "__main__":
    main()
