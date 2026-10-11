"""Match cached operator locators using published identities and street addresses."""

import json
import re
from collections import defaultdict
from pathlib import Path

from scripts.coordinates.match_osm import distinct_sites, identity, stations
from scripts.coordinates.matching import brand, normalized, station_name_keys, store_number
from scripts.coordinates.regional import normalized_address

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "data/coordinate-sources"
CHAINS = {
    "alltown_us",
    "hucks_us",
    "kwik_fill_us",
    "fast_stop_us",
    "cenex",
    "bucees_us",
    "bellstores_us",
    "royal_farms",
}


def operator_points():
    points = []
    for chain in sorted(CHAINS):
        data = json.loads((SOURCES / "minor-chains" / f"{chain}.json").read_text())
        for feature in data["features"]:
            tags = feature["properties"]
            geometry = feature.get("geometry")
            if not geometry or geometry.get("type") != "Point":
                continue
            full = tags.get("addr:full", "").split(",")
            city = tags.get("addr:city", full[-2].strip() if len(full) >= 3 else "")
            street = tags.get("addr:street_address", full[0] if len(full) >= 3 else "")
            name = tags.get("operator") or tags.get("name") or tags.get("brand", "")
            if chain == "hucks_us":
                number = re.match(r"hucks-(\d+)-", tags.get("ref", ""))
                if number:
                    name = f"Huck's #{number[1]}"
            elif chain == "royal_farms" and name.isdecimal():
                # The source's ref is its CMS ID; its visible name is the store ID.
                name = f"Royal Farms #{name}"
            points.append(
                {
                    "longitude": feature["geometry"]["coordinates"][0],
                    "latitude": feature["geometry"]["coordinates"][1],
                    "tags": {"name": name, "brand": tags.get("brand", ""), "addr:city": city},
                    "state": tags.get("addr:state", ""),
                    "street": street,
                    "source_url": tags["@source_uri"],
                    "external_id": tags.get("ref", feature["id"]),
                    "chain": chain,
                }
            )
    return points


def main():
    points = operator_points()
    by_place, by_street = defaultdict(list), defaultdict(list)
    for point in points:
        place = point["state"], normalized(point["tags"]["addr:city"])
        by_place[place].append(point)
        by_street[(*place, normalized_address(point["street"]))].append(point)
    corpay = json.loads((SOURCES / "fuelman-stations.json").read_text())
    by_name, by_number = defaultdict(list), defaultdict(list)
    for record in corpay:
        place = record["stateCode"], normalized(record["city"])
        for key in station_name_keys(record["name"], record["city"]):
            by_name[(*place, key)].append(record)
        by_number[(*place, brand(record["name"]), store_number(record["name"]))].append(record)
    accepted, ambiguous = [], []
    for identifier, row in stations().items():
        place = row["State"], normalized(row["City"])
        candidates = []
        for point in by_place[place]:
            if identity(row, point):
                candidates.append(
                    {**point, "evidence": [identity(row, point), "same_city", "same_state"]}
                )
        records = {}
        for name in row["names"]:
            named = [
                record
                for key in station_name_keys(name, row["City"])
                for record in by_name[(*place, key)]
            ]
            for record in named + by_number[(*place, brand(name), store_number(name))]:
                records[record["id"]] = record
        for record in records.values():
            rule = identity(
                row,
                {
                    "tags": {
                        "name": record["name"],
                        "brand": record.get("brandName", ""),
                        "addr:city": record["city"],
                    }
                },
            )
            if not rule:
                continue
            for point in by_street[(*place, normalized_address(record["address1"]))]:
                expected = {brand(name) for name in row["names"]} - {None}
                actual = {brand(point["tags"][key]) for key in ("name", "brand")} - {None}
                if expected and actual and not expected & actual:
                    continue
                candidates.append(
                    {
                        **point,
                        "evidence": [
                            rule,
                            "same_city",
                            "same_state",
                            {
                                "source_url": record["source_url"],
                                "external_id": record["id"],
                                "reason": "Published station identity supplies the exact street address matched to the operator locator.",
                            },
                        ],
                    }
                )
        sites = distinct_sites(candidates)
        if len(sites) != 1:
            if sites:
                ambiguous.append({"opis_id": identifier, "candidates": sites})
            continue
        point = sites[0]
        accepted.append(
            {
                "opis_id": identifier,
                "latitude": point["latitude"],
                "longitude": point["longitude"],
                "source": "operator_locator_with_station_identity",
                "source_url": point["source_url"],
                "external_id": point["external_id"],
                "matched_name": point["tags"]["name"],
                "matched_address": point["street"],
                "evidence": point["evidence"],
            }
        )
    for name, data in (
        ("minor-chains-candidates", accepted),
        ("minor-chains-ambiguous", ambiguous),
    ):
        (SOURCES / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print({"operator_points": len(points), "accepted": len(accepted), "ambiguous": len(ambiguous)})


if __name__ == "__main__":
    main()
