"""Cache public USDA retailer identities and match supplied fuel stations offline."""

import argparse
import csv
import io
import json
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import httpx

from scripts.coordinates.match_osm import distinct_sites, identity, stations
from scripts.coordinates.matching import brand, normalized, station_name_keys, store_number
from scripts.coordinates.regional import SNAP_ARCHIVE_URL, SNAP_QUERY_URL

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "data/coordinate-sources"
FIELDS = "Record_ID,Store_Name,Store_Street_Address,City,State,Latitude,Longitude,ObjectId"


def acquire():
    folder = SOURCES / "snap-current-pages"
    folder.mkdir(exist_ok=True)
    with httpx.Client(timeout=90) as client:
        offset = 0
        while True:
            path = folder / f"{offset:06}.json"
            if path.exists():
                payload = json.loads(path.read_text())
            else:
                time.sleep(1)
                response = client.get(
                    SNAP_QUERY_URL,
                    params={
                        "f": "json",
                        "where": "1=1",
                        "outFields": FIELDS,
                        "returnGeometry": "false",
                        "orderByFields": "ObjectId",
                        "resultOffset": offset,
                        "resultRecordCount": 1000,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                if "error" in payload or "features" not in payload:
                    raise ValueError(f"USDA query failed at offset {offset}.")
                temporary = path.with_suffix(".part")
                temporary.write_text(json.dumps(payload), encoding="utf-8")
                temporary.replace(path)
                print(
                    f"USDA current: cached {offset + len(payload['features'])} records", flush=True
                )
            if not payload.get("exceededTransferLimit", False):
                break
            if len(payload["features"]) != 1000:
                raise ValueError("USDA returned a truncated page.")
            offset += 1000
    current_records()


def current_records(folder=None):
    folder = folder or SOURCES / "snap-current-pages"
    paths = sorted(folder.glob("*.json"))
    if not paths:
        raise ValueError("USDA current export is missing.")
    records = []
    for page, path in enumerate(paths):
        if path.stem != f"{page * 1000:06}":
            raise ValueError("USDA current export has a missing or unexpected page.")
        payload = json.loads(path.read_text())
        features = payload.get("features", [])
        more = payload.get("exceededTransferLimit", False)
        if "error" in payload or (page < len(paths) - 1 and (not more or len(features) != 1000)):
            raise ValueError("USDA current export has an invalid page.")
        if page == len(paths) - 1 and more:
            raise ValueError("USDA current export is incomplete.")
        records.extend(item["attributes"] for item in features)
    if len({item["ObjectId"] for item in records}) != len(records):
        raise ValueError("USDA pages contain duplicate identities.")
    return records


def indexed_records(records, historical=False):
    by_name, by_number = defaultdict(list), defaultdict(list)
    for record in records:
        name = record["Store Name" if historical else "Store_Name"]
        state, city = record["State"], record["City"]
        place = state, normalized(city)
        point = {
            "latitude": float(record["Latitude"] or "nan"),
            "longitude": float(record["Longitude"] or "nan"),
            "tags": {"name": name, "addr:city": city},
            "record_id": str(record["Record ID" if historical else "Record_ID"]),
            "street": (record["Street Number"] + " " + record["Street Name"]).strip()
            if historical
            else record["Store_Street_Address"],
            "historical": historical,
        }
        for key in station_name_keys(name, city):
            by_name[(*place, key)].append(point)
        number = store_number(name)
        if number and brand(name):
            by_number[(*place, brand(name), number)].append(point)
    return by_name, by_number


def match():
    rows = stations()
    target_places = {(row["State"], normalized(row["City"])) for row in rows.values()}
    target_names = {
        (row["State"], normalized(row["City"]), key)
        for row in rows.values()
        for name in row["names"]
        for key in station_name_keys(name, row["City"])
    }
    with (
        zipfile.ZipFile(SOURCES / "regional-usda-snap-historical-2005-2025.zip") as archive,
        archive.open("Historical SNAP Retailer Locator Data 2005-2025.csv") as member,
    ):
        historical = [
            record
            for record in csv.DictReader(io.TextIOWrapper(member, encoding="utf-8-sig"))
            if (record["State"], normalized(record["City"])) in target_places
            and (
                brand(record["Store Name"]) is not None
                or any(
                    (record["State"], normalized(record["City"]), key) in target_names
                    for key in station_name_keys(record["Store Name"], record["City"])
                )
            )
        ]
    current_names, current_numbers = indexed_records(current_records())
    historic_names, historic_numbers = indexed_records(historical, historical=True)
    accepted, ambiguous = [], []
    for identifier, row in rows.items():
        place = row["State"], normalized(row["City"])
        options = []
        for names, numbers in (
            (current_names, current_numbers),
            (historic_names, historic_numbers),
        ):
            possible = {}
            for name in row["names"]:
                named = [
                    point
                    for key in station_name_keys(name, row["City"])
                    for point in names[(*place, key)]
                ]
                for point in named + numbers[(*place, brand(name), store_number(name))]:
                    possible[point["record_id"]] = point
            matches = [point for point in possible.values() if identity(row, point)]
            if matches:
                options = matches
                break
        sites = distinct_sites(options)
        if len(sites) != 1:
            if sites:
                ambiguous.append({"opis_id": identifier, "candidates": sites})
            continue
        point = sites[0]
        historic = point["historical"]
        url = (
            SNAP_ARCHIVE_URL
            if historic
            else SNAP_QUERY_URL
            + f"?f=json&where=Record_ID%3D{point['record_id']}&outFields=*&returnGeometry=true"
        )
        accepted.append(
            {
                "opis_id": identifier,
                "latitude": point["latitude"],
                "longitude": point["longitude"],
                "source": "historical_usda_snap_retailer_coordinate"
                if historic
                else "official_usda_snap_retailer_coordinate",
                "source_url": url,
                "external_id": f"usda:{point['record_id']}",
                "matched_name": point["tags"]["name"],
                "matched_address": point["street"],
                "evidence": [
                    identity(row, point),
                    "same_city",
                    "same_state",
                    "published_retailer_street_address_and_coordinate",
                    "Historical SNAP authorization dates describe benefit eligibility, not station closure."
                    if historic
                    else "current_usda_retailer_registration",
                ],
            }
        )
    for name, data in (("snap-identities", accepted), ("snap-ambiguous", ambiguous)):
        (SOURCES / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(
        {
            "historical_relevant_records": len(historical),
            "matched": len(accepted),
            "ambiguous": len(ambiguous),
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args()
    if args.fetch:
        acquire()
    match()
