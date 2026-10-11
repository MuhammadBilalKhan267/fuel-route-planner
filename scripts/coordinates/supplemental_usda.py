"""Extract selected historical store identities from the cached USDA directory."""

from __future__ import annotations

import argparse
import csv
import io
import json
import zipfile
from urllib.parse import urlencode

from loves_ta import DATA, PublicDownloads, write_json
from supplemental import brand


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", action="store_true")
    args = parser.parse_args()
    if args.current:
        download_current()
        return
    records = []
    path = DATA / "regional-usda-snap-historical-2005-2025.zip"
    with zipfile.ZipFile(path) as archive, archive.open(archive.namelist()[0]) as member:
        for row in csv.DictReader(io.TextIOWrapper(member, encoding="utf-8-sig")):
            if brand(row["Store Name"].strip()) is None:
                continue
            records.append(
                {
                    "external_id": row["Record ID"],
                    "name": row["Store Name"].strip(),
                    "address": f"{row['Street Number'].strip()} {row['Street Name'].strip()}".strip(),
                    "city": row["City"].strip(),
                    "state": row["State"].strip(),
                    "postal_code": row["Zip Code"],
                    "latitude": float(row["Latitude"]),
                    "longitude": float(row["Longitude"]),
                    "authorized_at": row["Authorization Date"],
                    "ended_at": row["End Date"],
                    "source_url": "https://fns-prod.azureedge.us/sites/default/files/resource-files/snap-retailer-locator-data2005-2025.zip",
                    "identity_kind": "historical_snap_retailer_address",
                }
            )
    write_json(DATA / "supplemental-usda-historical-stations.json", records)
    print({"historical_records": len(records)})


def download_current() -> None:
    path = DATA / "supplemental-usda-current-stations.json"
    if path.exists():
        print({"current_records": len(json.loads(path.read_text(encoding="utf-8"))), "requests": 0})
        return
    endpoint = "https://services1.arcgis.com/RLQu0rK7h4kbsBq5/arcgis/rest/services/snap_retailer_location_data/FeatureServer/0/query"
    prefixes = ("ALLSUP", "YESWAY", "CEFCO", "FIKES", "SHEETZ", "RUTTER", "RACETRAC", "RACE TRAC")
    where = " OR ".join(f"UPPER(Store_Name) LIKE '{prefix}%'" for prefix in prefixes)
    records, requests = [], []
    downloads = PublicDownloads()
    while True:
        url = (
            endpoint
            + "?"
            + urlencode(
                {
                    "f": "json",
                    "where": where,
                    "outFields": "*",
                    "returnGeometry": "false",
                    "resultRecordCount": 1000,
                    "resultOffset": len(records),
                    "orderByFields": "ObjectId",
                }
            )
        )
        result = json.loads(downloads.get(url))
        if "error" in result:
            raise ValueError(result["error"])
        page = [feature["attributes"] for feature in result["features"]]
        for record in page:
            record["source_url"] = url
        records.extend(page)
        requests.append({"source_url": url, "records": len(page)})
        if not result.get("exceededTransferLimit"):
            break
    downloads.client.close()
    write_json(path, records)
    write_json(
        DATA / "supplemental-usda-current-metadata.json",
        {"retrieved_at": "2026-10-11", "requests": requests},
    )
    print({"current_records": len(records), "requests": len(requests)})


if __name__ == "__main__":
    main()
