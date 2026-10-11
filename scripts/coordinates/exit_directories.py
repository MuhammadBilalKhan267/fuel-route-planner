"""Cache primary merchant directories and extract station identity evidence."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from scripts.coordinates.exit_anchors import csv_exit_request
from scripts.coordinates.match_osm import stations
from scripts.coordinates.matching import brand, normalized, station_name_keys, store_number

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "data/coordinate-sources"
DIRECTORIES = {
    "coam": "https://www.gacoam.com/API/Documents/Document?documentID=731",
    "nastc": "https://www.nastc.com/wp-content/PDFS/directories/NASTC_QPN_Stops_WEB.pdf",
}
MAX_BYTES = 15 * 1024 * 1024
CITY_ROW = re.compile(r"^\s*(.+?),\s+([A-Z]{2})\s{2,}(\S.*)$")
PHONE = re.compile(r"\((\d{3})\)\s*(\d{3})-(\d{4})")
ROUTE_ROW = re.compile(r"^\s*(\S.*?)\s{2,}([\d(][\d()\s-]+)\s*$")
US_STATES = frozenset(
    [
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
    ]
)


def nastc_records(text):
    """Preserve printed city/name/road/phone columns; never create coordinates."""
    records, rejected = [], []
    for page_number, page in enumerate(text.split("\f"), 1):
        pending = None
        for line in page.splitlines():
            city = CITY_ROW.match(line)
            if city:
                if pending:
                    rejected.append({**pending, "reason": "missing_route_phone_row"})
                pending = {
                    "city": city[1].strip(),
                    "state": city[2],
                    "name": city[3].strip(),
                    "page": page_number,
                    "raw_identity_row": line,
                }
                if city[2] not in US_STATES or (
                    normalized(city[1]) == "nowhere" and normalized(city[3]) == "test"
                ):
                    rejected.append({**pending, "reason": "non_us_or_publisher_test_record"})
                    pending = None
                continue
            route = ROUTE_ROW.match(line)
            if pending and route:
                phone = PHONE.fullmatch(route[2].strip())
                record = {
                    **pending,
                    "route_description": route[1].strip(),
                    "phone": "-".join(phone.groups()) if phone else None,
                    "raw_phone": route[2].strip(),
                    "phone_status": "parsed_printed_phone" if phone else "malformed_printed_phone",
                    "raw_route_row": line,
                    "source_url": DIRECTORIES["nastc"],
                    "coordinate_verified": False,
                    "purpose": "published_station_identity_only",
                }
                record["external_id"] = f"nastc:{page_number}:{len(records) + 1}"
                records.append(record)
                pending = None
        if pending:
            rejected.append({**pending, "reason": "missing_route_phone_row"})
    return records, rejected


def nastc_identity(row, record):
    """A name or numbered brand and city establish an identity proposal only."""
    if normalized(row["City"]) != normalized(record["city"]) or row["State"] != record["state"]:
        return None
    actual_number = store_number(record["name"])
    actual_brand = brand(record["name"])
    for name in row["names"]:
        number = store_number(name)
        if number and actual_number and number != actual_number:
            continue
        if actual_brand and actual_brand == brand(name):
            if number and number == actual_number:
                return "same_published_brand_store_number_city_state"
            continue
        names = station_name_keys(name) & station_name_keys(record["name"])
        if any(len(value) >= 7 for value in names):
            return "same_distinctive_name_city_state"
    return None


def write_nastc_proposals(records):
    unresolved = {
        int(row["opis_id"]) for row in json.loads((SOURCES / "unresolved.json").read_text())
    }
    proposals = []
    rows = stations()
    for identifier in sorted(unresolved):
        row = rows[identifier]
        for record in records:
            rule = nastc_identity(row, record)
            if not rule:
                continue
            original_exit = csv_exit_request(row["Address"])
            published_exit = csv_exit_request(record["route_description"])
            exact_exit = bool(
                original_exit
                and published_exit
                and original_exit["exit_ref"] == published_exit["exit_ref"]
                and set(original_exit["routes"]) & set(published_exit["routes"])
            )
            proposals.append(
                {
                    "opis_id": identifier,
                    "original_names": row["names"],
                    "original_city": row["City"],
                    "original_state": row["State"],
                    "original_address": row["Address"],
                    "primary_directory_record": record,
                    "identity_rule": rule,
                    "same_exact_published_exit": exact_exit,
                    "coordinate_verified": False,
                    "review_status": "requires_actual_premises_and_source_discrepancy_review",
                }
            )
    (SOURCES / "regional-exit-nastc-identity-proposals.json").write_text(
        json.dumps(proposals, indent=2) + "\n", encoding="utf-8"
    )
    print("NASTC identity proposals:", len(proposals))


def download(name, url):
    """One bounded acquisition per directory; preserve failures without retrying."""
    target = SOURCES / f"regional-exit-{name}-directory.pdf"
    ledger = SOURCES / f"regional-exit-{name}-acquisition.json"
    if target.exists():
        if not target.read_bytes().startswith(b"%PDF-"):
            raise ValueError("Cached source is not a PDF")
        return target
    if ledger.exists():
        raise RuntimeError("A prior acquisition exists; inspect it before any new request")
    if shutil.disk_usage(SOURCES).free < 512 * 1024 * 1024 + MAX_BYTES:
        raise RuntimeError("Insufficient free disk space for bounded PDF acquisition")
    evidence = {"source_url": url, "retrieved_at": "2026-10-11", "status": "started"}
    with ledger.open("x", encoding="utf-8") as handle:
        json.dump(evidence, handle, indent=2)
    started = time.monotonic()
    body = bytearray()
    try:
        with (
            httpx.Client(
                timeout=60,
                follow_redirects=True,
                headers={
                    "User-Agent": "SpotterAssessment-StationResearch/1.0 (cached public directories)"
                },
            ) as client,
            client.stream("GET", url) as response,
        ):
            evidence["http_status"] = response.status_code
            response.raise_for_status()
            if int(response.headers.get("Content-Length", 0)) > MAX_BYTES:
                raise ValueError("Declared PDF exceeds the download cap")
            for chunk in response.iter_bytes():
                if len(body) + len(chunk) > MAX_BYTES:
                    raise ValueError("PDF exceeds the download cap")
                if time.monotonic() - started > 90:
                    raise TimeoutError("PDF acquisition exceeded the elapsed-time cap")
                body.extend(chunk)
        if not body.startswith(b"%PDF-"):
            raise ValueError("Source returned non-PDF content")
        target.write_bytes(body)
        evidence.update(status="complete", sha256=hashlib.sha256(body).hexdigest())
    except Exception as error:
        evidence.update(status="failed", error_type=type(error).__name__, error=str(error)[:300])
        raise
    finally:
        evidence.update(
            downloaded_bytes=len(body), elapsed_seconds=round(time.monotonic() - started, 3)
        )
        ledger.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return target


def extract_text(name, path):
    target = SOURCES / f"regional-exit-{name}-directory.txt"
    if not target.exists():
        executable = shutil.which("pdftotext")
        if not executable:
            raise RuntimeError("pdftotext is required for read-only directory extraction")
        subprocess.run([executable, "-layout", str(path), str(target)], check=True, timeout=90)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Acquire each public PDF once")
    parser.add_argument("--directory", choices=DIRECTORIES, help="Process one selected directory")
    args = parser.parse_args()
    for name, url in DIRECTORIES.items():
        if args.directory and name != args.directory:
            continue
        path = SOURCES / f"regional-exit-{name}-directory.pdf"
        if args.download:
            path = download(name, url)
        if path.exists():
            text_path = extract_text(name, path)
            print(name, path.stat().st_size, "PDF bytes;", text_path.stat().st_size, "text bytes")
            if name == "nastc":
                records, rejected = nastc_records(text_path.read_text(encoding="utf-8"))
                for suffix, value in (("identities", records), ("unparsed", rejected)):
                    (SOURCES / f"regional-exit-nastc-{suffix}.json").write_text(
                        json.dumps(value, indent=2) + "\n", encoding="utf-8"
                    )
                print("NASTC printed records:", len(records), "; unparsed:", len(rejected))
                write_nastc_proposals(records)


if __name__ == "__main__":
    main()
