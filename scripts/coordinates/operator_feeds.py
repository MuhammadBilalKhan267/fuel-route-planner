"""Cache selected public operator locators published by AllThePlaces.

The default is offline. This prepares source evidence; it does not certify points.
"""

import argparse
import json
import time
from datetime import UTC, datetime

import httpx

from scripts.coordinates.match_osm import SOURCES

RUN = "2026-10-03-13-32-35"
FEEDS = (
    "bp",
    "gulf_pr_us",
    "mapco_us",
    "sinclair_us",
    "bellstores_us",
    "royal_farms",
    "eg_america_us",
    "refuel_double_quick_us",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    folder = SOURCES / "minor-chains"
    folder.mkdir(exist_ok=True)
    with httpx.Client(timeout=90, follow_redirects=True) as client:
        for name in FEEDS:
            path = folder / f"{name}.json"
            if path.exists() or not args.download:
                print({"feed": name, "cached": path.exists()}, flush=True)
                continue
            time.sleep(1)
            url = f"https://alltheplaces-data.openaddresses.io/runs/{RUN}/output/{name}.geojson"
            response = client.get(url)
            response.raise_for_status()
            data = response.json()
            if data.get("type") != "FeatureCollection" or not data.get("features"):
                raise ValueError(f"Missing operator features: {name}")
            features = []
            for feature in data["features"]:
                properties = feature["properties"]
                if properties.get("addr:country") != "US":
                    continue
                feature["properties"] = {
                    key: value
                    for key, value in properties.items()
                    if not key.startswith("contact:")
                    and key not in {"phone", "email", "fax", "opening_hours"}
                }
                features.append(feature)
            data["features"] = features
            data["acquisition"] = {
                "source_url": url,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "license": "AllThePlaces CC0; original operator source retained in each record",
            }
            with path.open("x", encoding="utf-8") as handle:
                json.dump(data, handle)
            print({"feed": name, "us_records": len(features)}, flush=True)


if __name__ == "__main__":
    main()
