"""Cache public Fuelman diesel locations and match station identities offline."""

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import httpx

from scripts.coordinates.match_osm import distinct_sites, identity, same_place, stations
from scripts.coordinates.matching import brand, normalized, station_name_keys, store_number

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "data/coordinate-sources"
URL = "https://sitelocator.fuelman.com/bin/sl/search"
PARAMS = {
    "network": "fuelman",
    "bounds": "18,-180,72,-65",
    "zoom": "5",
    "rows": "100000",
    "fq": "serviceTypes:Diesel",
}
FIELDS = {
    "id",
    "name",
    "brandName",
    "address1",
    "address2",
    "city",
    "stateCode",
    "zipCode",
    "highway",
    "exitNumber",
    "lat",
    "lng",
    "serviceTypes",
    "tags",
}


def station_records(payload, source_url):
    total = int(payload["totalHits"])
    docs = payload["docs"]
    if len(docs) != total or len({item["id"] for item in docs}) != total:
        raise ValueError("Fuelman response contained duplicate or missing records.")
    return [
        {**{key: value for key, value in item.items() if key in FIELDS}, "source_url": source_url}
        for item in docs
    ]


def acquire(network="fuelman"):
    output = SOURCES / f"{network}-stations.json"
    if output.exists():
        print("Fuelman records already cached; no network request.")
        return
    # The public API echoes 'start' but ignores it; request the complete set once.
    with httpx.Client(timeout=90, follow_redirects=False) as client:
        response = client.get(URL, params={**PARAMS, "network": network})
        response.raise_for_status()
    records = station_records(response.json(), str(response.url))
    output.write_text(json.dumps(records), encoding="utf-8")
    (SOURCES / f"{network}-acquisition.json").write_text(
        json.dumps(
            {
                "source_url": str(response.url),
                "retrieved_at": datetime.now(UTC).isoformat(),
                "records": len(records),
                "pagination": "single_complete_response",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def match(network="fuelman"):
    records = json.loads((SOURCES / f"{network}-stations.json").read_text(encoding="utf-8"))
    anchors = {
        normalized(item["city"]) + "|" + item["state"]: item
        for item in json.loads((SOURCES / "city-search-anchors.json").read_text()).values()
    }
    names = defaultdict(list)
    numbers = defaultdict(list)
    for record in records:
        point = {
            "latitude": record["lat"],
            "longitude": record["lng"],
            "record": record,
            "tags": {
                "name": record["name"],
                "brand": record.get("brandName", ""),
                "addr:city": record["city"],
            },
        }
        for key in station_name_keys(record["name"], record["city"]):
            names[(record["stateCode"], key)].append(point)
        chains = {brand(record["name"])} - {None}
        operator = brand(record.get("brandName", ""))
        if operator and operator not in {
            "shell",
            "exxon",
            "bp",
            "cenex",
            "chevron",
            "sinclair",
            "marathon",
            "sunoco",
            "texaco",
            "citgo",
            "amoco",
            "valero",
            "phillips66",
            "conoco",
            "union76",
            "arco",
        }:
            chains.add(operator)
        number = store_number(record["name"])
        if number:
            for chain in chains:
                numbers[(record["stateCode"], chain, number)].append(point)
    accepted, ambiguous = [], []
    for identifier, row in stations().items():
        possible = {}
        for name in row["names"]:
            named = [
                point
                for key in station_name_keys(name, row["City"])
                for point in names[(row["State"], key)]
            ]
            for point in named + numbers[(row["State"], brand(name), store_number(name))]:
                possible[point["record"]["id"]] = point
        matches = []
        for point in possible.values():
            rule, place = identity(row, point), same_place(row, point, anchors)
            if rule and place:
                matches.append(
                    {
                        **point,
                        "evidence": [rule, place, "same_state", "public_diesel_station_record"],
                    }
                )
        sites = distinct_sites(matches)
        if len(sites) != 1:
            if sites:
                ambiguous.append({"opis_id": identifier, "candidates": sites})
            continue
        point = sites[0]
        record = point["record"]
        accepted.append(
            {
                "opis_id": identifier,
                "latitude": point["latitude"],
                "longitude": point["longitude"],
                "source": f"{network}_public_station_locator",
                "source_url": record["source_url"],
                "external_id": record["id"],
                "matched_name": record["name"],
                "matched_address": ", ".join(
                    [record["address1"], record["city"], record["stateCode"]]
                ),
                "evidence": point["evidence"],
                "original_names": row["names"],
                "original_address": row["Address"],
                "original_city": row["City"],
                "original_state": row["State"],
            }
        )
    for name, data in (("candidates", accepted), ("ambiguous", ambiguous)):
        (SOURCES / f"{network}-{name}.json").write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )
    print({"fuelman_records": len(records), "accepted": len(accepted), "ambiguous": len(ambiguous)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--network", choices=("fuelman", "cfn"), default="fuelman")
    options = parser.parse_args()
    if options.download:
        acquire(options.network)
    match(options.network)


if __name__ == "__main__":
    main()
