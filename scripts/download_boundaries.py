"""Download public Census state boundaries once; never called by the API."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/0/query"
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("trips/data/us-states.geojson"))
    args = parser.parse_args()
    parameters = {
        "where": "STATE < '60'",
        "outFields": "STATE,STUSAB,NAME",
        "outSR": "4326",
        "returnGeometry": "true",
        "maxAllowableOffset": "0.0001",
        "geometryPrecision": "6",
        "f": "geojson",
    }
    request = Request(
        BASE + "?" + urlencode(parameters), headers={"User-Agent": "FuelRouteAssessment/0.1"}
    )
    with urlopen(request, timeout=120) as response:
        payload = json.load(response)
    if payload.get("type") != "FeatureCollection" or len(payload.get("features", [])) != 51:
        raise RuntimeError("Expected all 50 states and DC; boundary file was not replaced.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"Saved 51 Census state boundaries to {args.output}")


if __name__ == "__main__":
    main()
