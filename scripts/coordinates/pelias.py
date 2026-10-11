"""Cache bounded local venue searches for manual station identity review."""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from django.conf import settings

from scripts.coordinates.matching import brand, normalized, store_number

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "data/coordinate-sources"
PROPERTIES = {
    "id",
    "gid",
    "source",
    "source_id",
    "layer",
    "name",
    "label",
    "country",
    "country_a",
    "region",
    "region_a",
    "locality",
    "localadmin",
    "street",
    "housenumber",
    "postalcode",
    "confidence",
    "accuracy",
}


def local_parameters(row, anchor):
    latitude, longitude = anchor["latitude"], anchor["longitude"]
    return {
        "text": f"{row['names'][0]}, {row['city']}, {row['state']}",
        "boundary.country": "USA",
        "boundary.rect.min_lat": max(18, latitude - 0.35),
        "boundary.rect.max_lat": min(72, latitude + 0.35),
        "boundary.rect.min_lon": max(-180, longitude - 0.5),
        "boundary.rect.max_lon": min(-65, longitude + 0.5),
        "layers": "venue",
        "size": 5,
    }


def sanitize(payload):
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": item.get("geometry"),
                "properties": {
                    key: value
                    for key, value in item.get("properties", {}).items()
                    if key in PROPERTIES
                },
            }
            for item in payload.get("features", [])
        ],
    }


def main(limit, fetch):
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fuelroute.settings")
    rows = json.loads((SOURCES / "unresolved.json").read_text())
    anchors = json.loads((SOURCES / "city-search-anchors.json").read_text())
    folder = SOURCES / "pelias-venues"
    folder.mkdir(exist_ok=True)
    # Distinctive independent names make this probe useful; numbered chain
    # identities already have more reliable bulk operator directories.
    rows.sort(
        key=lambda row: (
            bool(brand(row["names"][0])),
            bool(store_number(row["names"][0])),
            -len(row["names"][0]),
            row["opis_id"],
        )
    )
    requests, cached, venues = 0, 0, 0
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for row in rows:
            anchor = anchors.get(normalized(row["city"]) + "|" + row["state"])
            if not anchor:
                continue
            parameters = local_parameters(row, anchor)
            digest = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()[
                :12
            ]
            path = folder / f"{row['opis_id']}-{digest}.json"
            if path.exists():
                data = json.loads(path.read_text())
                cached += 1
            elif fetch and requests < limit:
                if not settings.ORS_API_KEY:
                    raise ValueError("ORS_API_KEY is required for venue preparation.")
                if requests:
                    time.sleep(4)
                response = client.get(
                    settings.ORS_API_BASE_URL + "/pelias/v1/search",
                    headers={"Authorization": settings.ORS_API_KEY},
                    params=parameters,
                )
                requests += 1
                if response.status_code != 200:
                    raise ValueError(
                        f"Venue preparation stopped: HTTP {response.status_code}; no retry."
                    )
                remaining = response.headers.get("x-ratelimit-remaining")
                data = {
                    "opis_id": row["opis_id"],
                    "source_url": settings.ORS_API_BASE_URL
                    + "/pelias/v1/search?"
                    + urlencode(parameters),
                    "parameters": parameters,
                    "remaining_daily_quota": remaining,
                    "response": sanitize(response.json()),
                    "review_status": "unreviewed_search_results_not_verified_coordinates",
                }
                temporary = path.with_suffix(".part")
                temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                temporary.replace(path)
                if remaining and int(remaining) < 200:
                    print(
                        "Stopped with fewer than 200 daily geocoding calls remaining.", flush=True
                    )
                    break
            else:
                continue
            count = len(data["response"]["features"])
            venues += count
            print({"opis_id": row["opis_id"], "venues": count, "requests": requests}, flush=True)
    print({"requests": requests, "cache_hits": cached, "venue_proposals": venues})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--limit", type=int, choices=range(1, 1001), default=40)
    args = parser.parse_args()
    main(args.limit, args.fetch)
