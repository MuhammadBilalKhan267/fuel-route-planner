"""Match price-CSV stations to CAT Scale's official public location download.

The location is a physical scale on the truck-stop premises. A brand/name match,
city and state, and highway/exit or street-address evidence are all required.
Closed locations retain coordinates but are marked unavailable for planning.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import math
import re
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

from pilot import US_STATES, normalized

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "data" / "coordinate-sources"
SOURCE_URL = "https://catscale.com/exl.php"
SOURCE_FIELDS = (
    "CATScaleNumber",
    "State",
    "InterstateCity",
    "TruckstopName",
    "InterstateAddress",
    "InterstatePostalCode",
    "Latitude",
    "Longitude",
    "URL",
)
BRANDS = (
    ("flying_j", r"FLYING\s*J"),
    ("pilot", r"PILOT"),
    ("one9", r"ONE\s*9"),
    ("loves", r"LOVE'?S"),
    ("petro", r"\bPETRO\b(?!\s*MART)"),
    ("ta", r"\bTA\b|TRAVEL\s*CENT[ER]+S?\s*(?:OF\s*)?AMERICA"),
    ("speedway", r"SPEEDWAY"),
    ("road_ranger", r"ROAD\s*RANGER"),
    ("mr_fuel", r"MR\.?\s*FUEL"),
    ("kwik_trip", r"KWIK\s*TRIP"),
    ("kwik_star", r"KWIK\s*STAR"),
    ("quiktrip", r"QUIK\s*TRIP"),
    ("caseys", r"CASEY'?S"),
    ("shell", r"\bSHELL\b"),
    ("circle_k", r"CIRCLE\s*K"),
    ("sapp_bros", r"SAPP\s*BROS"),
    ("racetrac", r"RACE\s*TRAC"),
    ("ambest", r"AM\s*BEST"),
    ("maverik", r"MAVERIK"),
)


def brand_names(text: str) -> set[str]:
    return {brand for brand, pattern in BRANDS if re.search(pattern, text.upper())}


def core_name(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).upper()
    text = re.sub(r"TEMP\s*CLOSED|PERMANENTLY\s*CLOSED|\bCLOSED\b", "", text)
    text = re.sub(r"#\s*\d+", "", text)
    text = re.sub(
        r"\b(?:TRUCK\s*STOP|TRUCKSTOP|TRAVEL|CENTERS?|CENTRES?|PLAZA|STORE|THE|OF|DBA|INC)\b",
        " ",
        text,
    )
    return " ".join(re.findall(r"[A-Z0-9]+", text))


def matching_name(left: str, right: str) -> bool:
    left_numbers = set(re.findall(r"#\s*(\d+)", left))
    right_numbers = set(re.findall(r"#\s*(\d+)", right))
    if left_numbers and right_numbers and not left_numbers & right_numbers:
        return False
    left_brands, right_brands = brand_names(left), brand_names(right)
    if left_brands or right_brands:
        return bool(left_brands & right_brands)
    left_core, right_core = core_name(left), core_name(right)
    if not left_core or not right_core:
        return False
    return (
        left_core == right_core
        or difflib.SequenceMatcher(None, left_core, right_core).ratio() >= 0.9
    )


def road_details(text: str) -> tuple[set[str], set[str]]:
    text = text.upper().replace("INTERSTATE", "I-")
    highways = set()
    for match in re.finditer(r"\b(?:I\s*[- ]?\s*|1[- ]+)(\d+(?:\s*[/&]\s*\d+)*)", text):
        highways.update(re.findall(r"\d+", match.group(1)))
    exits = {
        str(int(number)) + suffix
        for number, suffix in re.findall(r"\bEX(?:IT)?\s*(\d+)(?:\s*([A-D])\b)?", text)
    }
    return highways, exits


def location_matches(row: dict, source: dict) -> list[str]:
    left_highways, left_exits = road_details(row["Address"])
    right_highways, right_exits = road_details(source["address"])
    if left_highways & right_highways and left_exits & right_exits:
        return ["same_interstate_and_exit"]
    if normalized(row["Address"]) == normalized(source["address"]):
        return ["same_street_or_highway_address"]
    return []


def download(fetch: bool) -> list[dict]:
    path = OUTPUT / "catscale-download.csv"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        if not fetch:
            raise SystemExit("Missing CAT Scale download; use --fetch to acquire it.")
        try:
            with urllib.request.urlopen(SOURCE_URL, timeout=30) as response:
                content = response.read().decode("utf-8-sig")
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"CAT Scale HTTP {exc.code}; stopped without retries.") from exc
        rows = list(csv.DictReader(content.splitlines()))
    else:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    # Omit contact-person fields; only station matching data is retained.
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SOURCE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    sources = []
    for row in rows:
        if row["State"] not in US_STATES:
            continue
        latitude, longitude = float(row["Latitude"]), float(row["Longitude"])
        if not all(math.isfinite(value) for value in (latitude, longitude)):
            raise ValueError(f"Invalid coordinate for scale {row['CATScaleNumber']}")
        sources.append(
            {
                "external_id": row["CATScaleNumber"],
                "name": row["TruckstopName"],
                "city": row["InterstateCity"],
                "state": row["State"],
                "address": row["InterstateAddress"],
                "postal_code": row["InterstatePostalCode"],
                "latitude": latitude,
                "longitude": longitude,
                "source_url": SOURCE_URL,
                "station_url": row["URL"],
                "availability": "closed" if "CLOSED" in row["TruckstopName"].upper() else "unknown",
            }
        )
    return sources


def match_stations(sources: list[dict], csv_path: Path) -> tuple[list[dict], list[dict]]:
    by_city = defaultdict(list)
    for source in sources:
        by_city[(source["state"], normalized(source["city"]))].append(source)
    stations = defaultdict(list)
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["State"] in US_STATES:
                stations[int(row["OPIS Truckstop ID"])].append(row)
    accepted, unresolved = [], []
    for opis_id, variants in sorted(stations.items()):
        candidates = {}
        for row in variants:
            for source in by_city[(row["State"], normalized(row["City"]))]:
                if not matching_name(row["Truckstop Name"], source["name"]):
                    continue
                evidence = location_matches(row, source)
                if evidence:
                    candidates[source["external_id"]] = (source, evidence)
        if len(candidates) == 1:
            source, evidence = next(iter(candidates.values()))
            accepted.append(
                {
                    "opis_id": opis_id,
                    "latitude": source["latitude"],
                    "longitude": source["longitude"],
                    "source": "catscale_official_location",
                    "source_url": SOURCE_URL,
                    "external_id": source["external_id"],
                    "matched_name": source["name"],
                    "matched_address": ", ".join(
                        (source["address"], source["city"], source["state"], source["postal_code"])
                    ),
                    "evidence": [
                        "same_brand_or_station_name",
                        "same_city",
                        "same_state",
                        *evidence,
                    ],
                    "availability": source["availability"],
                    "coordinate_kind": "scale_on_station_premises",
                    "original_names": sorted({row["Truckstop Name"] for row in variants}),
                    "original_address": variants[0]["Address"],
                    "original_city": variants[0]["City"],
                    "original_state": variants[0]["State"],
                }
            )
        else:
            nearby = by_city[(variants[0]["State"], normalized(variants[0]["City"]))]
            if nearby:
                unresolved.append(
                    {
                        "opis_id": opis_id,
                        "names": sorted({row["Truckstop Name"] for row in variants}),
                        "address": variants[0]["Address"],
                        "city": variants[0]["City"],
                        "state": variants[0]["State"],
                        "reason": "ambiguous_identity"
                        if candidates
                        else "no_corroborated_identity",
                        "candidates": list(candidates),
                    }
                )
    used_sources = defaultdict(list)
    for candidate in accepted:
        used_sources[candidate["external_id"]].append(candidate)
    collisions = {
        candidate["opis_id"]
        for candidates in used_sources.values()
        if len(candidates) > 1
        for candidate in candidates
    }
    for candidate in accepted:
        if candidate["opis_id"] in collisions:
            unresolved.append(
                {
                    "opis_id": candidate["opis_id"],
                    "names": candidate["original_names"],
                    "address": candidate["original_address"],
                    "city": candidate["original_city"],
                    "state": candidate["original_state"],
                    "reason": "same_scale_matched_multiple_opis_ids",
                    "candidates": [candidate["external_id"]],
                }
            )
    return [
        candidate for candidate in accepted if candidate["opis_id"] not in collisions
    ], unresolved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--csv", type=Path, default=ROOT / "fuel-prices-for-be-assessment.csv")
    args = parser.parse_args()
    sources = download(args.fetch)
    accepted, unresolved = match_stations(sources, args.csv)
    for name, records in (
        ("catscale-stations.json", sources),
        ("catscale-candidates.json", accepted),
        ("catscale-unresolved.json", unresolved),
    ):
        (OUTPUT / name).write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "official_us_locations": len(sources),
                "accepted": len(accepted),
                "closed_candidates": sum(row["availability"] == "closed" for row in accepted),
                "unresolved_in_source_cities": len(unresolved),
            }
        )
    )


if __name__ == "__main__":
    main()
