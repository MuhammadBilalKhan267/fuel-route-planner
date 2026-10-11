"""Cache public EPA UST identities; published points require separate premises review.

The national source contains historical state records. Its tank status does not
establish whether a retail station is currently open or closed.
"""

import argparse
import json
import time
from collections import defaultdict
from datetime import UTC, datetime

import httpx

from scripts.coordinates.match_osm import SOURCES, identity, stations
from scripts.coordinates.matching import normalized

SERVICE = (
    "https://services.arcgis.com/cJ9YHowT8TU7DUyn/ArcGIS/rest/services/"
    "UST_Finder_Feature_Layer_2eb/FeatureServer/0"
)
FIELDS = (
    "OBJECTID,Facility_ID,Name,Address,City,State,Zip_Code,Coordinate_Source,"
    "Address_Match_Type,Facility_Status,Latitude,Longitude"
)


def quoted(value):
    return "'" + value.replace("'", "''") + "'"


def main(download):
    rows = stations()
    states = {
        feature["properties"]["STUSAB"]: feature["properties"]["NAME"]
        for feature in json.loads(
            (SOURCES.parents[1] / "trips/data/us-states.geojson").read_text()
        )["features"]
    }
    cache = SOURCES / "epa-ust-pages"
    cache.mkdir(exist_ok=True)
    query_path = cache / "queries.json"
    if not query_path.exists():
        targets = {
            item["opis_id"]
            for filename in ("unresolved.json", "conflicts.json")
            for item in json.loads((SOURCES / filename).read_text())
        }
        cities = defaultdict(set)
        for identifier in targets:
            row = rows[identifier]
            if row["State"] in states:
                cities[row["State"]].add(row["City"].strip().upper())
        queries = []
        for state, values in sorted(cities.items()):
            ordered = sorted(values)
            for start in range(0, len(ordered), 60):
                chunk = ordered[start : start + 60]
                queries.append(
                    "State="
                    + quoted(states[state])
                    + " AND UPPER(City) IN ("
                    + ",".join(map(quoted, chunk))
                    + ")"
                )
        query_path.write_text(json.dumps(queries, indent=2) + "\n", encoding="utf-8")
    queries = json.loads(query_path.read_text())
    records, requests = {}, 0
    with httpx.Client(timeout=45) as client:
        for query_index, where in enumerate(queries):
            offset, query_ids = 0, set()
            while True:
                path = cache / f"query-{query_index:03d}-{offset:06d}.json"
                if not path.exists():
                    if not download:
                        print({"pending_public_query": query_index, "use": "--download"})
                        return
                    time.sleep(1)
                    response = client.get(
                        SERVICE + "/query",
                        params={
                            "f": "json",
                            "where": where,
                            "outFields": FIELDS,
                            "outSR": 4326,
                            "resultRecordCount": 2000,
                            "resultOffset": offset,
                            "orderByFields": "OBJECTID ASC",
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if "error" in payload or "features" not in payload:
                        raise ValueError("Public EPA query failed; acquisition stopped.")
                    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
                    requests += 1
                payload = json.loads(path.read_text())
                features = payload["features"]
                identifiers = [item["attributes"]["OBJECTID"] for item in features]
                if len(set(identifiers)) != len(identifiers) or query_ids.intersection(identifiers):
                    raise ValueError("EPA pagination returned duplicate facility IDs.")
                query_ids.update(identifiers)
                for feature in features:
                    item = feature["attributes"]
                    records[item["OBJECTID"]] = item
                if not payload.get("exceededTransferLimit"):
                    break
                if len(features) != 2000:
                    raise ValueError("EPA returned an incomplete paginated facility page.")
                offset += len(features)
            print(
                {
                    "completed_query": query_index + 1,
                    "queries": len(queries),
                    "facilities": len(records),
                },
                flush=True,
            )
    inverse_states = {name: abbreviation for abbreviation, name in states.items()}
    addresses = []
    by_place = defaultdict(list)
    for record in records.values():
        state = inverse_states.get(record["State"])
        if not state:
            continue
        item = {
            "name": record["Name"] or "",
            "street": record["Address"] or "",
            "city": record["City"] or "",
            "state": state,
            "source_url": SERVICE,
            "external_id": str(record["Facility_ID"]),
            "latitude": record["Latitude"],
            "longitude": record["Longitude"],
            "coordinate_source": record["Coordinate_Source"],
            "historical_tank_status": record["Facility_Status"],
            "coordinate_verified": False,
        }
        addresses.append(item)
        by_place[state, normalized(item["city"])].append(item)
    identities = []
    for identifier, row in rows.items():
        for item in by_place[row["State"], normalized(row["City"])]:
            if identity(row, {"tags": {"name": item["name"], "addr:city": item["city"]}}):
                identities.append({"opis_id": identifier, **item})
    for name, value in (
        ("epa-ust-addresses.json", addresses),
        ("epa-ust-identities.json", identities),
    ):
        (SOURCES / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    (SOURCES / "epa-ust-acquisition.json").write_text(
        json.dumps(
            {
                "source_url": SERVICE,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "requests_this_run": requests,
                "queries": len(queries),
                "facilities": len(records),
                "station_identity_matches": len(identities),
                "coordinate_candidates": 0,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        {
            "facilities": len(records),
            "station_identity_matches": len(identities),
            "requests": requests,
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    main(parser.parse_args().download)
