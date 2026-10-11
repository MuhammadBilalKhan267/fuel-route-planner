"""Submit recovered Quarles streets once to Census for search anchors only."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
from datetime import UTC, datetime

from loves_ta import DATA, PublicDownloads, normalized, write_json
from supplemental import targets

ENDPOINT = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"


def inputs() -> tuple[list[list[str]], list[dict]]:
    guide = json.loads(
        (DATA / "supplemental-quarles-address-guide.json").read_text(encoding="utf-8")
    )
    rows, unresolved = [], []
    for row in targets():
        if not normalized(row["Truckstop Name"]).startswith("QUARLES"):
            continue
        number = re.search(r"#\s*(\d+)", row["Truckstop Name"])
        identities = {
            identity["street_and_zip"]: identity
            for identity in guide
            if number and int(identity["store_number"]) == int(number[1])
        }
        if len(identities) != 1:
            unresolved.append(
                {
                    "opis_id": int(row["OPIS Truckstop ID"]),
                    "reason": "Official guide does not provide one unique numbered street",
                }
            )
            continue
        identity = next(iter(identities.values()))
        match = re.fullmatch(r"(.*)\s+(\d{5})", identity["street_and_zip"])
        if not match or not re.match(r"\d+[A-Z]?\s", match[1], flags=re.IGNORECASE):
            unresolved.append(
                {
                    "opis_id": int(row["OPIS Truckstop ID"]),
                    "reason": "Official guide street lacks a numeric house number and ZIP",
                }
            )
            continue
        rows.append([row["OPIS Truckstop ID"], match[1], identity["city"], row["State"], match[2]])
    return rows, unresolved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    rows, unresolved = inputs()
    source = DATA / "supplemental-quarles-census-input.csv"
    result = DATA / "supplemental-quarles-census-result.csv"
    if not result.exists():
        if not args.download:
            raise SystemExit("Cached Census batch is missing; use --download once")
        with source.open("w", encoding="utf-8", newline="") as stream:
            csv.writer(stream).writerows(rows)
        downloads = PublicDownloads()
        with source.open("rb") as stream:
            response = downloads.client.post(
                ENDPOINT,
                files={"addressFile": (source.name, stream, "text/csv")},
                data={"benchmark": "Public_AR_Current"},
                timeout=300,
            )
        if response.status_code in {401, 403, 429}:
            raise SystemExit(f"Stopped Census request: HTTP {response.status_code}")
        response.raise_for_status()
        result.write_text(response.text, encoding="utf-8")
        write_json(
            DATA / "supplemental-quarles-census-metadata.json",
            {
                "source_url": ENDPOINT,
                "submitted_count": len(rows),
                "retrieved_at": datetime.now(UTC).isoformat(),
                "anchor_only": True,
                "benchmark": "Public_AR_Current",
            },
        )
    anchors = []
    for fields in csv.reader(io.StringIO(result.read_text(encoding="utf-8"))):
        row = {
            "opis_id": int(fields[0]),
            "input_address": fields[1],
            "match_status": fields[2],
            "source_url": ENDPOINT,
            "anchor_only": True,
        }
        if fields[2] == "Match":
            longitude, latitude = fields[5].split(",")
            row.update(
                match_type=fields[3],
                matched_address=fields[4],
                longitude=float(longitude),
                latitude=float(latitude),
            )
        anchors.append(row)
    write_json(DATA / "supplemental-quarles-census-anchors.json", anchors)
    write_json(DATA / "supplemental-quarles-guide-missing.json", unresolved)
    print(
        {
            "submitted": len(anchors),
            "matched_search_anchors": sum(row["match_status"] == "Match" for row in anchors),
            "missing_guide_identity": len(unresolved),
        }
    )


if __name__ == "__main__":
    main()
