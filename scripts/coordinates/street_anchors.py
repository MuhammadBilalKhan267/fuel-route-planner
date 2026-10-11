"""Cache Census street-address anchors for unresolved, published retailer identities.

These anchors support manual premises review and are never coordinate candidates.
"""

import argparse
import csv
import io
import json
from collections import defaultdict
from datetime import UTC, datetime

import httpx

from scripts.coordinates.match_osm import identity, stations
from scripts.coordinates.matching import brand, normalized, station_name_keys, store_number
from scripts.coordinates.premises import numeric_street, published_identities
from scripts.coordinates.regional import normalized_address
from scripts.coordinates.snap import SOURCES

ENDPOINT = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"


def directory_identities(targets, rows):
    by_name, by_number = defaultdict(list), defaultdict(list)
    for record in published_identities():
        if not numeric_street(record["street"]):
            continue
        place = record["state"], normalized(record["city"])
        for key in station_name_keys(record["name"], record["city"]):
            by_name[(*place, key)].append(record)
        by_number[(*place, brand(record["name"]), store_number(record["name"]))].append(record)
    accepted, ambiguous = [], []
    for identifier in sorted(targets):
        row = rows[identifier]
        place = row["State"], normalized(row["City"])
        addresses = defaultdict(list)
        for name in row["names"]:
            named = [
                record
                for key in station_name_keys(name, row["City"])
                for record in by_name[(*place, key)]
            ]
            for record in named + by_number[(*place, brand(name), store_number(name))]:
                if identity(row, {"tags": {"name": record["name"], "addr:city": record["city"]}}):
                    addresses[normalized_address(record["street"])].append(record)
        if len(addresses) != 1:
            if addresses:
                ambiguous.append({"opis_id": identifier, "addresses": dict(addresses)})
            continue
        records = next(iter(addresses.values()))
        record = records[0]
        accepted.append(
            {
                "opis_id": identifier,
                "matched_name": record["name"],
                "matched_address": record["street"],
                "source_url": record["source_url"],
                "external_id": record["external_id"],
                "evidence": ["same_station_identity", "same_city", "same_state"],
            }
        )
    return accepted, ambiguous


def main(submit, directories=False, network_audit=False):
    prefix = (
        "network-audit-census"
        if network_audit
        else "directory-street-census"
        if directories
        else "street-census"
    )
    source = SOURCES / f"{prefix}-input.csv"
    output = SOURCES / f"{prefix}-result.csv"
    if not source.exists():
        targets = {
            item["opis_id"]
            for filename in ("unresolved.json", "conflicts.json")
            for item in json.loads((SOURCES / filename).read_text())
        }
        rows = stations()
        if network_audit:
            network = {
                str(record["id"]): record
                for record in json.loads((SOURCES / "fuelman-stations.json").read_text())
            }
            identities = [
                {
                    "opis_id": item["opis_id"],
                    "matched_name": network[item["external_id"]]["name"],
                    "matched_address": network[item["external_id"]]["address1"],
                    "source_url": item["source_url"],
                    "external_id": item["external_id"],
                    "latitude": item["latitude"],
                    "longitude": item["longitude"],
                    "evidence": item["evidence"],
                }
                for item in json.loads((SOURCES / "merged.json").read_text())
                if item["source"]
                in {"fuelman_public_station_locator", "cfn_public_station_locator"}
                and item["external_id"] in network
                and numeric_street(network[item["external_id"]]["address1"])
            ]
        elif directories:
            identities, ambiguous = directory_identities(targets, rows)
            (SOURCES / f"{prefix}-ambiguous-identities.json").write_text(
                json.dumps(ambiguous, indent=2) + "\n", encoding="utf-8"
            )
        else:
            identities = [
                item
                for item in json.loads((SOURCES / "snap-identities.json").read_text())
                if item["opis_id"] in targets and numeric_street(item["matched_address"])
            ]
        if len({item["opis_id"] for item in identities}) != len(identities):
            raise ValueError("Published identities contain duplicate station IDs.")
        with source.open("w", encoding="utf-8", newline="") as stream:
            csv.writer(stream).writerows(
                [
                    item["opis_id"],
                    item["matched_address"],
                    rows[item["opis_id"]]["City"].strip(),
                    rows[item["opis_id"]]["State"],
                    "",
                ]
                for item in identities
            )
        (SOURCES / f"{prefix}-identity-evidence.json").write_text(
            json.dumps(identities, indent=2) + "\n", encoding="utf-8"
        )
    if not output.exists():
        if not submit:
            print(f"Prepared {source.name}; use --submit for one public address batch.")
            return
        with (
            httpx.Client(timeout=300, follow_redirects=False) as client,
            source.open("rb") as stream,
        ):
            response = client.post(
                ENDPOINT,
                files={"addressFile": (source.name, stream, "text/csv")},
                data={"benchmark": "Public_AR_Current"},
            )
            response.raise_for_status()
            output.write_text(response.text, encoding="utf-8")
        (SOURCES / f"{prefix}-acquisition.json").write_text(
            json.dumps(
                {
                    "source_url": ENDPOINT,
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "requests": 1,
                    "benchmark": "Public_AR_Current",
                    "anchor_only": True,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    anchors = []
    for fields in csv.reader(io.StringIO(output.read_text(encoding="utf-8"))):
        item = {
            "opis_id": int(fields[0]),
            "input_address": fields[1],
            "status": fields[2],
            "anchor_only": True,
        }
        if fields[2] == "Match":
            longitude, latitude = fields[5].split(",")
            item.update(
                match_type=fields[3],
                matched_address=fields[4],
                longitude=float(longitude),
                latitude=float(latitude),
            )
        anchors.append(item)
    with source.open(encoding="utf-8", newline="") as stream:
        identifiers = {int(fields[0]) for fields in csv.reader(stream)}
    if {item["opis_id"] for item in anchors} != identifiers or len(anchors) != len(identifiers):
        raise ValueError(
            "Census response does not account for every submitted identity exactly once."
        )
    (SOURCES / f"{prefix}-anchors.json").write_text(
        json.dumps(anchors, indent=2) + "\n", encoding="utf-8"
    )
    print(
        {
            "submitted": len(anchors),
            "matched_search_anchors": sum(item["status"] == "Match" for item in anchors),
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submit", action="store_true")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--directories", action="store_true")
    group.add_argument("--network-audit", action="store_true")
    args = parser.parse_args()
    main(args.submit, args.directories, args.network_audit)
