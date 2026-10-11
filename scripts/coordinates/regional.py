"""Extract station coordinates from cached official regional-chain locators."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import struct
import time
import zipfile
import zlib
from pathlib import Path

from pyproj import Geod

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "data" / "coordinate-sources"
US_STATES = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
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
    "DC",
}
SNAP_QUERY_URL = (
    "https://services1.arcgis.com/RLQu0rK7h4kbsBq5/arcgis/rest/services/"
    "snap_retailer_location_data/FeatureServer/0/query"
)
SNAP_ARCHIVE_URL = (
    "https://fns-prod.azureedge.us/sites/default/files/resource-files/"
    "snap-retailer-locator-data2005-2025.zip"
)


def normalized_city(value: str) -> str:
    words = value.upper().replace("SAINT ", "ST ").replace("FORT ", "FT ")
    words = words.replace("MOUNT ", "MT ")
    return re.sub(r"[^A-Z0-9]", "", words)


def normalized_address(value: str) -> str:
    value = value.upper()
    replacements = {
        "NORTHEAST": "NE",
        "NORTHWEST": "NW",
        "SOUTHEAST": "SE",
        "SOUTHWEST": "SW",
        "NORTH": "N",
        "SOUTH": "S",
        "EAST": "E",
        "WEST": "W",
        "STREET": "ST",
        "AVENUE": "AVE",
        "ROAD": "RD",
        "BOULEVARD": "BLVD",
        "DRIVE": "DR",
        "PARKWAY": "PKWY",
        "HIGHWAY": "HWY",
        "LANE": "LN",
        "COURT": "CT",
        "CIRCLE": "CIR",
    }
    for word, abbreviation in replacements.items():
        value = re.sub(rf"\b{word}\b", abbreviation, value)
    value = re.sub(r"\bUS[ -]+HWY\b", "HWY", value)
    value = re.sub(r"\b[A-Z]{2} \d{5}(?:-\d{4})?\b", "", value)
    return re.sub(r"[^A-Z0-9]", "", value)


def write_json(name: str, value: object) -> None:
    (SOURCES / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def merchant_address_directories() -> None:
    """Parse cached verified PDF columns; directory coordinates remain review evidence."""
    nrha = []
    with (SOURCES / "regional-nrha-merchant-table.tsv").open(encoding="utf-8", newline="") as table:
        for row in csv.DictReader(table, delimiter="\t"):
            nrha.append(
                {
                    **row,
                    "page": int(row["page"]),
                    "source_url": "https://nrha.com/media/pdf/partner/nfuelloc.pdf",
                }
            )
    write_json("regional-nrha-merchant-addresses.json", nrha)
    write_json(
        "regional-nrha-kum-and-go-addresses.json",
        [row for row in nrha if re.search(r"KUM\s*&\s*GO", row["name"], re.IGNORECASE)],
    )
    rooftops = []
    text = (SOURCES / "regional-mcf-rooftop-inventory.txt").read_text(encoding="utf-8")
    for page, content in enumerate(text.split("\f"), 1):
        for line in content.splitlines():
            fields = re.split(r"\s{2,}", line.strip())
            if len(fields) < 9 or not re.match(r"CASEY", fields[0], re.IGNORECASE):
                continue
            if fields[3] not in US_STATES:
                continue
            try:
                latitude, longitude = float(fields[6]), float(fields[7])
            except ValueError:
                continue
            rooftops.append(
                {
                    "name": fields[0],
                    "address": fields[1],
                    "city": fields[2],
                    "state": fields[3],
                    "postal_code": fields[4],
                    "county": fields[5],
                    "latitude": latitude,
                    "longitude": longitude,
                    "external_id": fields[8],
                    "source_url": "https://www.mcfcommunications.com/wp-content/uploads/2020/12/MCF_UpdatedSiteList_10-1-20.pdf",
                    "source_page": page,
                }
            )
    write_json("regional-mcf-caseys-rooftops.json", rooftops)
    print(f"Directories: {len(nrha)} merchant addresses; {len(rooftops)} Casey rooftop records")


def download_kum_archive() -> None:
    """Retrieve only the historical locator ZIP member, never its full archive."""
    import httpx

    manifest = json.loads(
        (SOURCES / "regional-kum-atp-archive-source.json").read_text(encoding="utf-8")
    )
    target = SOURCES / "regional-alltheplaces-kum_and_go_us-2024-10-19.geojson"
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != manifest["sha256"]:
            raise ValueError("Cached historical Kum & Go export differs from its source manifest")
        return
    entry = manifest["entry"]
    if entry["compression"] != 8:
        raise ValueError("Expected a deflate-compressed ZIP member")
    with httpx.Client(
        timeout=45, headers={"User-Agent": "AssessmentStationResearch/1.0"}
    ) as client:

        def byte_range(start: int, size: int) -> bytes:
            with client.stream(
                "GET", manifest["url"], headers={"Range": f"bytes={start}-{start + size - 1}"}
            ) as response:
                if response.status_code != 206:
                    raise ValueError(
                        f"Archive server rejected bounded range retrieval: HTTP {response.status_code}"
                    )
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=min(size, 8192)):
                    content.extend(chunk)
                    if len(content) > size:
                        raise ValueError("Archive server exceeded the requested byte range")
                if len(content) != size:
                    raise ValueError("Archive byte range length differs from requested length")
                return bytes(content)

        header = struct.unpack("<4s5H3I2H", byte_range(entry["offset"], 30))
        if header[0] != b"PK\x03\x04" or header[3] != 8:
            raise ValueError("Historical archive member header is invalid")
        time.sleep(1.05)
        payload = byte_range(
            entry["offset"] + 30 + header[-2] + header[-1], entry["compressed_size"]
        )
    content = zlib.decompress(payload, -15)
    if len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != manifest["sha256"]:
        raise ValueError("Historical archive member failed size or SHA-256 verification")
    target.write_bytes(content)


def retailer_identity(name: str) -> tuple[str, str] | None:
    """Read published store numbers without assuming every directory uses '#'."""
    number = re.search(r"(\d+)(?:-?[A-Z])?\s*(?:\([^)]*\))?$", name.strip(), re.IGNORECASE)
    if not number:
        return None
    if re.search(r"\bKUM\s*(?:&|AND)\s*GO\b", name, re.IGNORECASE):
        return "kumandgo", str(int(number[1]))
    identity = chain_identity(f"{name[: number.start()]} #{number[1]}")
    if identity and identity[0] == "stripes" and re.search(r"\bSTARS\b", name, re.IGNORECASE):
        return None
    return identity


def snap_locations() -> list[dict]:
    """Normalize the official FNS current export, retaining its record identity."""
    records = []
    for path in sorted(SOURCES.glob("regional-usda-snap-api-[0-9][0-9][0-9][0-9][0-9].json")):
        records.extend(
            feature["attributes"]
            for feature in json.loads(path.read_text(encoding="utf-8"))["features"]
        )
    write_json("regional-usda-current-stations.json", records)
    locations = []
    for record in records:
        identity = retailer_identity(record["Store_Name"])
        if not identity or record["State"] not in US_STATES:
            continue
        latitude, longitude = record["Latitude"], record["Longitude"]
        if (
            latitude is None
            or longitude is None
            or not (-180 <= longitude <= 180 and -90 <= latitude <= 90)
        ):
            continue
        locations.append(
            {
                "brand": identity[0],
                "external_id": identity[1],
                "retailer_record_id": str(record["Record_ID"]),
                "name": record["Store_Name"],
                "address": record["Store_Street_Address"],
                "city": record["City"],
                "state": record["State"],
                "postal_code": record["Zip_Code"],
                "latitude": latitude,
                "longitude": longitude,
                "source_url": SNAP_QUERY_URL
                + f"?f=json&where=Record_ID%3D{record['Record_ID']}&outFields=*&returnGeometry=true",
                "coordinate_source": "official_usda_snap_retailer_coordinate",
            }
        )
    return locations


def snap_historical_locations() -> list[dict]:
    """Stream the official 24 MB archive; do not expand its full CSV on disk."""
    brands = re.compile(
        r"MAVERIK|KWIK (?:TRIP|STAR)|QUIKTRIP|CASEY|SPEEDWAY|7[- ]ELEVEN|STRIPES|CIRCLE K|KUM",
        re.IGNORECASE,
    )
    with (
        zipfile.ZipFile(SOURCES / "regional-usda-snap-historical-2005-2025.zip") as archive,
        archive.open("Historical SNAP Retailer Locator Data 2005-2025.csv") as member,
    ):
        records = [
            record
            for record in csv.DictReader(io.TextIOWrapper(member, encoding="utf-8-sig"))
            if brands.search(record["Store Name"])
        ]
    write_json("regional-usda-historical-stations.json", records)
    locations = []
    for record in records:
        identity = retailer_identity(record["Store Name"])
        if not identity or record["State"] not in US_STATES:
            continue
        try:
            latitude, longitude = float(record["Latitude"]), float(record["Longitude"])
        except (ValueError, TypeError):
            continue
        locations.append(
            {
                "brand": identity[0],
                "external_id": identity[1],
                "retailer_record_id": record["Record ID"],
                "name": record["Store Name"].strip(),
                "address": f"{record['Street Number'].strip()} {record['Street Name'].strip()}".strip(),
                "city": record["City"],
                "state": record["State"],
                "latitude": latitude,
                "longitude": longitude,
                "authorization_date": record["Authorization Date"],
                "end_date": record["End Date"].strip(),
                "source_url": SNAP_ARCHIVE_URL,
            }
        )
    return locations


def crosscheck_snap(locations: list[dict], retailers: list[dict]) -> None:
    """Require unique store/address identities and expose disputed coordinates."""
    by_identity, official = {}, {}
    for location in retailers:
        key = (
            location["brand"],
            location["external_id"],
            normalized_city(location["city"]),
            location["state"],
        )
        by_identity.setdefault(key, []).append(location)
    for location in locations:
        official[location["brand"], str(int(location["external_id"]))] = location
    geod = Geod(ellps="WGS84")
    candidates, review = [], []
    for opis_id, station in csv_stations().items():
        identity = chain_identity(station["Truckstop Name"])
        if not identity:
            continue
        matches = by_identity.get(
            (*identity, normalized_city(station["City"]), station["State"]), []
        )
        if not matches:
            continue
        addresses = {normalized_address(match["address"]) for match in matches}
        if len(addresses) != 1:
            review.append(
                {
                    "opis_id": int(opis_id),
                    "matches": matches,
                    "reason": "different_addresses_for_same_store_number",
                }
            )
            continue
        match = matches[0]
        current = official.get(identity)
        if not current:
            review.append(
                {
                    "opis_id": int(opis_id),
                    "station": match,
                    "reason": "independent_current_locator_missing",
                }
            )
            continue
        if normalized_address(match["address"]) != normalized_address(current["address"]):
            review.append(
                {
                    "opis_id": int(opis_id),
                    "station": match,
                    "official_station": current,
                    "reason": "independent_current_street_address_differs",
                }
            )
            continue
        comparisons = matches[1:] + ([current] if current else [])
        disagreements = []
        for other in comparisons:
            distance = geod.inv(
                match["longitude"], match["latitude"], other["longitude"], other["latitude"]
            )[2]
            if distance > 400:
                disagreements.append({"station": other, "distance_meters": round(distance, 2)})
        if disagreements:
            review.append(
                {
                    "opis_id": int(opis_id),
                    "station": match,
                    "reason": "coordinate_disagreement",
                    "disagreements": disagreements,
                }
            )
            continue
        candidates.append(
            {
                "opis_id": int(opis_id),
                "latitude": current["latitude"],
                "longitude": current["longitude"],
                "source": "official_locator_crosschecked_usda_store_id_address_and_coordinate",
                "source_url": current["source_url"],
                "external_id": f"{identity[0]}:{identity[1]}",
                "matched_name": match["name"],
                "matched_address": match["address"],
                "evidence": {
                    "csv_name": station["Truckstop Name"],
                    "store_number": identity[1],
                    "brand_match": True,
                    "city_match": True,
                    "state_match": True,
                    "usda_record_ids": [row["retailer_record_id"] for row in matches],
                    "usda_source_url": match["source_url"],
                    "exact_normalized_street_match": True,
                    "coordinate_agreement_meters": round(
                        geod.inv(
                            match["longitude"],
                            match["latitude"],
                            current["longitude"],
                            current["latitude"],
                        )[2],
                        2,
                    ),
                    "retrieved_on": "2026-10-11",
                },
            }
        )
    write_json("regional-usda-candidates.json", candidates)
    write_json("regional-usda-crosscheck.json", review)
    print(f"USDA current: {len(candidates)} station confirmations; {len(review)} need review")


def crosscheck_kum_and_go(locations: list[dict], historical: list[dict]) -> None:
    """Bridge old Kum & Go numbers to Maverik by exact published premises."""
    old_stores = [row for row in historical if row["brand"] == "kumandgo"]
    archive_path = SOURCES / "regional-alltheplaces-kum_and_go_us-2024-10-19.geojson"
    for feature in json.loads(archive_path.read_text(encoding="utf-8"))["features"]:
        properties = feature["properties"]
        number = properties["ref"].removeprefix("KG")
        longitude, latitude = feature["geometry"]["coordinates"]
        old_stores.append(
            {
                "brand": "kumandgo",
                "external_id": str(int(number)),
                "name": f"Kum & Go #{number}",
                "address": properties["addr:street_address"],
                "city": properties["addr:city"],
                "state": properties["addr:state"],
                "latitude": latitude,
                "longitude": longitude,
                "source_url": properties["@source_uri"],
                "archive_url": "https://alltheplaces-data.openaddresses.io/runs/2024-10-19-13-32-19/output.zip",
                "archive_member": "output/kum_and_go_us.geojson",
                "snapshot_date": "2024-10-19",
            }
        )
    for filename in (
        "regional-nrha-merchant-addresses.json",
        "supplemental-comdata-addresses.json",
    ):
        path = SOURCES / filename
        if not path.exists():
            continue
        for row in json.loads(path.read_text(encoding="utf-8")):
            identity = retailer_identity(row["name"])
            if identity and identity[0] == "kumandgo":
                old_stores.append({**row, "brand": "kumandgo", "external_id": identity[1]})
    by_identity, by_address = {}, {}
    for old in old_stores:
        key = old["external_id"], normalized_city(old["city"]), old["state"]
        by_identity.setdefault(key, []).append(old)
    for current in locations:
        if current["brand"] != "maverik":
            continue
        key = (
            normalized_address(current["address"]),
            normalized_city(current["city"]),
            current["state"],
        )
        by_address.setdefault(key, []).append(current)
    geod = Geod(ellps="WGS84")
    candidates, review = [], []
    for opis_id, station in csv_stations().items():
        identity = retailer_identity(station["Truckstop Name"])
        if not identity or identity[0] != "kumandgo":
            continue
        old = by_identity.get((identity[1], normalized_city(station["City"]), station["State"]), [])
        matches = {}
        for record in old:
            key = (
                normalized_address(record["address"]),
                normalized_city(record["city"]),
                record["state"],
            )
            for current in by_address.get(key, []):
                matches.setdefault(current["external_id"], (current, []))[1].append(record)
        if len(matches) != 1:
            review.append(
                {
                    "opis_id": int(opis_id),
                    "csv_station": station,
                    "historical_stations": old,
                    "reason": "current_exact_address_missing_or_ambiguous",
                }
            )
            continue
        current, bridges = next(iter(matches.values()))
        distances = []
        for old in bridges:
            if "latitude" in old:
                distances.append(
                    geod.inv(
                        old["longitude"], old["latitude"], current["longitude"], current["latitude"]
                    )[2]
                )
        if distances and max(distances) > 400:
            review.append(
                {
                    "opis_id": int(opis_id),
                    "current_station": current,
                    "historical_stations": bridges,
                    "reason": "coordinate_disagreement",
                    "distance_meters": round(max(distances), 2),
                }
            )
            continue
        candidates.append(
            {
                "opis_id": int(opis_id),
                "latitude": current["latitude"],
                "longitude": current["longitude"],
                "source": "current_official_maverik_crosschecked_historical_kum_and_go_store_id",
                "source_url": current["source_url"],
                "external_id": f"maverik:{current['external_id']}",
                "matched_name": current["name"],
                "matched_address": current["address"],
                "evidence": {
                    "csv_name": station["Truckstop Name"],
                    "historical_store_id": identity[1],
                    "exact_normalized_street_match": True,
                    "city_match": True,
                    "state_match": True,
                    "historical_sources": bridges,
                    "coordinate_agreement_meters": round(max(distances), 2) if distances else None,
                    "retrieved_on": "2026-10-11",
                },
            }
        )
    identities = {}
    for candidate in candidates:
        identities.setdefault(candidate["external_id"], []).append(candidate)
    ambiguous = [
        candidate for group in identities.values() if len(group) > 1 for candidate in group
    ]
    for candidate in ambiguous:
        review.append(
            {**candidate, "reason": "different_historical_identities_share_one_current_store"}
        )
    candidates = [candidate for candidate in candidates if candidate not in ambiguous]
    write_json("regional-kum-and-go-candidates.json", candidates)
    write_json("regional-kum-and-go-crosscheck.json", review)
    print(f"Kum & Go: {len(candidates)} Maverik premises confirmations; {len(review)} need review")


def kwiktrip_locations() -> list[dict]:
    locations = []
    text = (SOURCES / "regional-kwiktrip-indexed-table-complete.txt").read_text(encoding="utf-8")
    for line in text.splitlines():
        line = re.sub(r"cite\d+†(.*?)", r"\1", line)
        fields = [part.strip() for part in re.sub(r"^L\d+: ", "", line).split("|")]
        if len(fields) != 16 or not fields[0].isdigit():
            continue
        if not re.match(r"KWIK (?:TRIP|STAR) #", fields[1]):
            continue
        locations.append(
            {
                "brand": "kwiktrip",
                "external_id": fields[0],
                "name": fields[1],
                "address": fields[2],
                "city": fields[3],
                "state": fields[4],
                "postal_code": fields[5],
                "latitude": float(fields[7]),
                "longitude": float(fields[8]),
                "diesel": fields[11] == "Yes",
                "source_url": "https://www.kwiktrip.com/Maps-Downloads/Store-List",
                "coordinate_source": "official_published_store_table",
            }
        )
    return locations


def quiktrip_locations() -> list[dict]:
    locations = {}
    for path in sorted(SOURCES.glob("regional-quiktrip-api-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for result in data["response"]["results"]:
            record = result["data"]
            address = record["address"]
            point = record.get("yextDisplayCoordinate")
            if address["countryCode"] != "US" or not point:
                continue
            locations[record["id"]] = {
                "brand": "quiktrip",
                "external_id": record["id"],
                "name": f"QuikTrip #{record['id']}",
                "address": address["line1"],
                "city": address["city"],
                "state": address["region"],
                "postal_code": address["postalCode"],
                "latitude": point["latitude"],
                "longitude": point["longitude"],
                "source_url": "https://locations.quiktrip.com/search",
                "coordinate_source": "official_locator_display_coordinate",
                "provider_uid": record.get("uid"),
            }
    return list(locations.values())


def maverik_locations() -> list[dict]:
    locations = {}
    geod = Geod(ellps="WGS84")
    paths = [
        SOURCES / "regional-maverik-api-all.json",
        *sorted(SOURCES.glob("regional-maverik-api-[0-9][0-9][0-9][0-9].json")),
    ]
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        for entity in data["response"]["entities"]:
            profile = entity["profile"]
            address = profile["address"]
            point = profile.get("yextRoutableCoordinate") or profile.get("yextDisplayCoordinate")
            if address["countryCode"] != "US" or not point:
                continue
            number = profile["meta"]["id"]
            if not number.isdigit():
                continue
            locations[number] = {
                "brand": "maverik",
                "external_id": number,
                "name": f"Maverik #{number}",
                "address": address["line1"],
                "city": address["city"],
                "state": address["region"],
                "postal_code": address["postalCode"],
                "latitude": point["lat"],
                "longitude": point["long"],
                "source_url": profile.get("c_pagesURL")
                or ("https://locations.maverik.com/" + entity["url"].lstrip("/")),
                "coordinate_source": "official_locator_routable_coordinate"
                if profile.get("yextRoutableCoordinate")
                else "official_locator_display_coordinate",
                "display_coordinate": profile.get("yextDisplayCoordinate"),
                "routable_coordinate": profile.get("yextRoutableCoordinate"),
                "coordinate_fields_distance_meters": (
                    round(
                        geod.inv(
                            profile["yextDisplayCoordinate"]["long"],
                            profile["yextDisplayCoordinate"]["lat"],
                            profile["yextRoutableCoordinate"]["long"],
                            profile["yextRoutableCoordinate"]["lat"],
                        )[2],
                        2,
                    )
                    if profile.get("yextDisplayCoordinate")
                    and profile.get("yextRoutableCoordinate")
                    else 0
                ),
            }
    return list(locations.values())


def circlek_locations() -> list[dict]:
    locations = {}
    for path in sorted(SOURCES.glob("regional-circlek-api-*.json")):
        for record in json.loads(path.read_text(encoding="utf-8")):
            if record["brand"] != "CIRCLEK" or record["address"]["country"] != "US":
                continue
            address = record["address"]
            number = record["id"]
            locations[number] = {
                "brand": "circlek",
                "external_id": number,
                "name": f"Circle K #{number}",
                "address": address["street"],
                "city": address["city"],
                "state": address["state"],
                "postal_code": address["postalCode"],
                "latitude": record["latitude"],
                "longitude": record["longitude"],
                "source_url": "https://api.circlek.com/us/ngrp-store-locator/v1/stations",
                "coordinate_source": "official_locator_station_coordinate",
            }
    return list(locations.values())


def caseys_locations() -> list[dict]:
    data = json.loads((SOURCES / "regional-alltheplaces-caseys.html").read_text(encoding="utf-8"))
    locations = {}
    for feature in data["features"]:
        properties = feature["properties"]
        if properties.get("addr:country") != "US":
            continue
        number = properties["ref"].removesuffix("_fuel").removesuffix("_store")
        if not number.isdigit():
            continue
        longitude, latitude = feature["geometry"]["coordinates"]
        locations[number] = {
            "brand": "caseys",
            "external_id": number,
            "name": f"Casey's #{number}",
            "address": properties["addr:street_address"],
            "city": properties["addr:city"],
            "state": properties["addr:state"],
            "postal_code": properties.get("addr:postcode", ""),
            "latitude": latitude,
            "longitude": longitude,
            "source_url": properties["website"],
            "coordinate_source": "official_locator_via_alltheplaces",
            "dataset_url": "https://alltheplaces-data.openaddresses.io/runs/2026-09-19-13-32-18/output/caseys_general_store_us.geojson",
            "original_source_url": properties["@source_uri"],
        }
    return list(locations.values())


def speedway_and_seven_locations() -> list[dict]:
    locations = {}
    for spider, run in (
        ("seven_eleven_ca_us", "2026-10-03-13-32-35"),
        ("speedway_us", "2026-10-03-13-32-35"),
    ):
        data = json.loads(
            (SOURCES / f"regional-alltheplaces-{spider}.geojson").read_text(encoding="utf-8")
        )
        known_brands = {
            str(feature["properties"]["ref"]).removesuffix("_SHOP"): feature["properties"]["brand"]
            for feature in data["features"]
            if feature["properties"].get("brand") in ("7-Eleven", "Speedway", "Stripes")
        }
        for feature in data["features"]:
            properties = feature["properties"]
            if properties.get("addr:country") != "US":
                continue
            number = str(properties["ref"]).removesuffix("_SHOP")
            if not number.isdigit():
                continue
            brand_name = properties.get("brand") or known_brands.get(number)
            brand = {"Speedway": "speedway", "7-Eleven": "seveneleven", "Stripes": "stripes"}.get(
                brand_name
            )
            if not brand:
                continue
            longitude, latitude = feature["geometry"]["coordinates"]
            locations[brand, number] = {
                "brand": brand,
                "external_id": number,
                "name": f"{brand} #{number}",
                "address": properties.get("addr:street_address", ""),
                "city": properties.get("addr:city", ""),
                "state": properties.get("addr:state", ""),
                "postal_code": properties.get("addr:postcode", ""),
                "latitude": latitude,
                "longitude": longitude,
                "source_url": properties.get("website", properties["@source_uri"]),
                "coordinate_source": "official_locator_via_alltheplaces",
                "dataset_url": f"https://alltheplaces-data.openaddresses.io/runs/{run}/output/{spider}.geojson",
                "original_source_url": properties["@source_uri"],
            }
    return list(locations.values())


def crosscheck_speedway_archive(locations: list[dict]) -> None:
    archive_path = SOURCES / "pilot-one9-archive.json"
    candidate_path = SOURCES / "pilot-archive-candidates.json"
    if not archive_path.exists() or not candidate_path.exists():
        return
    archived = {
        station["external_id"]: station
        for station in json.loads(archive_path.read_text(encoding="utf-8"))["stations"]
    }
    by_address = {}
    for location in locations:
        if location["brand"] != "speedway":
            continue
        key = (
            normalized_address(location["address"]),
            normalized_city(location["city"]),
            location["state"],
        )
        by_address.setdefault(key, []).append(location)
    geod = Geod(ellps="WGS84")
    candidates, review = [], []
    for previous in json.loads(candidate_path.read_text(encoding="utf-8")):
        old = archived[previous["external_id"]]
        if old["name"] != "Speedway":
            continue
        key = normalized_address(old["address"]), normalized_city(old["city"]), old["state"]
        matches = by_address.get(key, [])
        if len(matches) != 1:
            review.append(
                {
                    "opis_id": previous["opis_id"],
                    "old_station": old,
                    "reason": "current_address_not_unique_or_missing",
                }
            )
            continue
        current = matches[0]
        _, _, distance = geod.inv(
            old["longitude"], old["latitude"], current["longitude"], current["latitude"]
        )
        if distance > 400:
            review.append(
                {
                    "opis_id": previous["opis_id"],
                    "old_station": old,
                    "current_station": current,
                    "distance_meters": round(distance, 2),
                    "reason": "coordinate_disagreement",
                }
            )
            continue
        candidates.append(
            {
                "opis_id": previous["opis_id"],
                "latitude": current["latitude"],
                "longitude": current["longitude"],
                "source": "current_official_locator_crosschecked_archived_store_id",
                "source_url": current["source_url"],
                "external_id": f"speedway:{current['external_id']}",
                "matched_name": previous["matched_name"],
                "matched_address": current["address"],
                "evidence": {
                    "archived_external_id": old["external_id"],
                    "archived_source_url": old["source_url"],
                    "archived_source_year": old["source_year"],
                    "current_global_store_id": current["external_id"],
                    "exact_normalized_street_match": True,
                    "city_match": True,
                    "state_match": True,
                    "independent_coordinate_agreement_meters": round(distance, 2),
                    "dataset_url": current["dataset_url"],
                    "original_source_url": current["original_source_url"],
                    "retrieved_on": "2026-10-11",
                },
            }
        )
    write_json("regional-speedway-current-candidates.json", candidates)
    write_json("regional-speedway-crosscheck.json", review)
    print(
        f"Speedway archive: {len(candidates)} current location confirmations; {len(review)} need review"
    )


def crosscheck_sevenfleet() -> None:
    """Bridge historical Speedway IDs through exact current street addresses."""
    current_rows = list(
        csv.reader(
            (SOURCES / "regional-7fleet-2026-07-08.csv").read_text(encoding="cp1252").splitlines()
        )
    )
    current = [dict(zip(current_rows[2], row)) for row in current_rows[3:] if row[1] == "Open"]
    by_address = {}
    for row in current:
        key = normalized_address(row["Address"]), normalized_city(row["City"]), row["State"]
        by_address.setdefault(key, []).append(row)
    with (SOURCES / "regional-7fleet-2024-05-23.csv").open(
        encoding="utf-8-sig", newline=""
    ) as file:
        historical = list(csv.DictReader(file))
    identities = {}
    for row in historical:
        if row["Status"] != "Open":
            continue
        identity = chain_identity(row["Stop Name"])
        if identity:
            identities.setdefault(identity, []).append(row)
    geod = Geod(ellps="WGS84")
    candidates, review = [], []
    for opis_id, station in csv_stations().items():
        identity = chain_identity(station["Truckstop Name"])
        rows = identities.get(identity, [])
        if len(rows) != 1:
            continue
        old = rows[0]
        if station["State"] != old["State"] or normalized_city(station["City"]) != normalized_city(
            old["City"]
        ):
            review.append(
                {
                    "opis_id": int(opis_id),
                    "csv_station": station,
                    "historical_station": old,
                    "reason": "city_or_state_mismatch",
                }
            )
            continue
        key = normalized_address(old["Address"]), normalized_city(old["City"]), old["State"]
        matches = by_address.get(key, [])
        if len(matches) != 1:
            review.append(
                {
                    "opis_id": int(opis_id),
                    "csv_station": station,
                    "historical_station": old,
                    "reason": "current_exact_address_missing_or_ambiguous",
                }
            )
            continue
        new = matches[0]
        if not all(
            (
                old["Latitude"],
                old["Longitude"],
                new["Long Lat (Latitude)"],
                new["Long Lat (Longitude)"],
            )
        ):
            review.append(
                {
                    "opis_id": int(opis_id),
                    "historical_station": old,
                    "current_station": new,
                    "reason": "missing_coordinate",
                }
            )
            continue
        lat, lon = float(new["Long Lat (Latitude)"]), float(new["Long Lat (Longitude)"])
        _, _, distance = geod.inv(float(old["Longitude"]), float(old["Latitude"]), lon, lat)
        if distance > 400:
            review.append(
                {
                    "opis_id": int(opis_id),
                    "historical_station": old,
                    "current_station": new,
                    "distance_meters": round(distance, 2),
                    "reason": "coordinate_disagreement",
                }
            )
            continue
        candidates.append(
            {
                "opis_id": int(opis_id),
                "latitude": lat,
                "longitude": lon,
                "source": "official_7fleet_current_crosschecked_historical_store_id",
                "source_url": "https://7fleetnetwork.com/wp21/wp-content/uploads/2026/08/7Fleet_Locations_2026-07-08b.csv",
                "external_id": new["Store #"],
                "matched_name": new["Store: Store Name"],
                "matched_address": new["Address"],
                "evidence": {
                    "csv_name": station["Truckstop Name"],
                    "historical_store_id": old["Stop Number"],
                    "historical_source_url": "https://7fleetnetwork.com/wp21/wp-content/uploads/2024/05/7Fleet_Locations_2024-05-23.csv",
                    "exact_normalized_street_match": True,
                    "city_match": True,
                    "state_match": True,
                    "coordinate_agreement_meters": round(distance, 2),
                    "retrieved_on": "2026-10-11",
                },
            }
        )
    write_json("regional-7fleet-candidates.json", candidates)
    write_json("regional-7fleet-crosscheck.json", review)
    print(f"7Fleet: {len(candidates)} current station confirmations; {len(review)} need review")


def csv_stations() -> dict[str, dict]:
    stations = {}
    with (ROOT / "fuel-prices-for-be-assessment.csv").open(
        encoding="utf-8-sig", newline=""
    ) as file:
        for row in csv.DictReader(file):
            if row["State"] in US_STATES:
                stations.setdefault(row["OPIS Truckstop ID"], row)
    return stations


def chain_identity(name: str) -> tuple[str, str] | None:
    number = re.search(r"#\s*(\d+)", name)
    if not number:
        return None
    for brand, pattern in (
        ("kwiktrip", r"KWIK (?:TRIP|STAR)"),
        ("quiktrip", r"QUIKTRIP"),
        ("maverik", r"MAVERIK"),
        ("circlek", r"CIRCLE K"),
        ("caseys", r"CASEY['’]?S"),
        ("speedway", r"SPEEDWAY"),
        ("seveneleven", r"7[- ]?ELEVEN"),
        ("stripes", r"STRIPES"),
    ):
        if re.search(pattern, name, re.IGNORECASE):
            return brand, str(int(number[1]))
    return None


def match(locations: list[dict]) -> tuple[list[dict], list[dict]]:
    by_identity = {}
    for location in locations:
        if not location["external_id"].isdigit():
            continue
        identity = location["brand"], str(int(location["external_id"]))
        if identity in by_identity:
            raise ValueError(f"Duplicate official store identity: {identity}")
        by_identity[identity] = location
    candidates, unresolved = [], []
    for opis_id, station in csv_stations().items():
        identity = chain_identity(station["Truckstop Name"])
        if not identity:
            continue
        location = by_identity.get(identity)
        reason = None
        if not location:
            reason = "store_number_not_in_official_dataset"
        elif station["State"] != location["state"]:
            reason = "state_mismatch"
        elif normalized_city(station["City"]) != normalized_city(location["city"]):
            reason = "city_mismatch"
        elif location.get("coordinate_fields_distance_meters", 0) > 400:
            reason = "official_locator_coordinate_fields_disagree"
        if reason:
            unresolved.append(
                {
                    "opis_id": int(opis_id),
                    "csv_station": station,
                    "official_station": location,
                    "reason": reason,
                }
            )
            continue
        candidates.append(
            {
                "opis_id": int(opis_id),
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "source": location["coordinate_source"],
                "source_url": location["source_url"],
                "external_id": f"{identity[0]}:{identity[1]}",
                "matched_name": location["name"],
                "matched_address": location["address"],
                "evidence": {
                    "brand_match": True,
                    "store_number_match": True,
                    "city_match": True,
                    "state_match": True,
                    "csv_name": station["Truckstop Name"],
                    "csv_address": station["Address"],
                    "csv_city": station["City"],
                    "csv_state": station["State"],
                    "official_city": location["city"],
                    "official_state": location["state"],
                    "retrieved_on": "2026-10-11",
                    **(
                        {
                            "dataset_url": location["dataset_url"],
                            "original_source_url": location["original_source_url"],
                        }
                        if "dataset_url" in location
                        else {}
                    ),
                },
            }
        )
    return candidates, unresolved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--download-kum-archive",
        action="store_true",
        help="Fetch the missing historical Kum & Go ZIP member with two bounded public GETs.",
    )
    args = parser.parse_args()
    SOURCES.mkdir(parents=True, exist_ok=True)
    if args.download_kum_archive:
        download_kum_archive()
    merchant_address_directories()
    all_locations = []
    for brand, loader in (
        ("kwiktrip", kwiktrip_locations),
        ("quiktrip", quiktrip_locations),
        ("maverik", maverik_locations),
        ("circlek", circlek_locations),
        ("caseys", caseys_locations),
        ("speedway-seveneleven", speedway_and_seven_locations),
    ):
        locations = loader()
        write_json(f"regional-{brand}-locations.json", locations)
        all_locations.extend(locations)
        print(f"{brand}: {len(locations)} official locations")
    candidates, unresolved = match(all_locations)
    crosscheck_speedway_archive(all_locations)
    crosscheck_sevenfleet()
    retailers = snap_locations()
    crosscheck_snap(all_locations, retailers)
    historical = snap_historical_locations()
    crosscheck_kum_and_go(all_locations, historical)
    write_json("regional-candidates.json", candidates)
    write_json("regional-unresolved.json", unresolved)
    print(f"Matched {len(candidates)} CSV stations; {len(unresolved)} require further evidence")


if __name__ == "__main__":
    main()
