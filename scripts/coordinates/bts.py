"""Cache the public USDOT truck-parking inventory for station identity matching."""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

URL = (
    "https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/"
    "NTAD_Truck_Stop_Parking/FeatureServer/0/query"
)
OUTPUT = Path(__file__).resolve().parents[2] / "data/coordinate-sources/bts-truck-parking.json"


def main():
    if OUTPUT.exists():
        print("Public truck-parking inventory already cached; no network request.")
        return
    features = []
    with httpx.Client(timeout=60) as client:
        count_response = client.get(
            URL, params={"f": "json", "where": "1=1", "returnCountOnly": "true"}
        )
        count_response.raise_for_status()
        expected = count_response.json()["count"]
        print(f"Inventory count: {expected}", flush=True)
        for offset in range(0, expected, 2000):
            time.sleep(1)
            response = client.get(
                URL,
                params={
                    "f": "geojson",
                    "where": "1=1",
                    "outFields": "*",
                    "outSR": "4326",
                    "orderByFields": "OBJECTID",
                    "resultOffset": offset,
                    "resultRecordCount": "2000",
                },
            )
            response.raise_for_status()
            features.extend(response.json()["features"])
            print(f"Downloaded {len(features)}/{expected}", flush=True)
    identifiers = {feature["properties"]["OBJECTID"] for feature in features}
    if len(features) != expected or len(identifiers) != expected:
        raise RuntimeError("Public inventory pagination was incomplete or repeated records.")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("x", encoding="utf-8") as handle:
        json.dump(
            {
                "type": "FeatureCollection",
                "features": features,
                "acquisition": {
                    "source": URL,
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "license": "USDOT public-domain data",
                },
            },
            handle,
            ensure_ascii=False,
        )


if __name__ == "__main__":
    main()
