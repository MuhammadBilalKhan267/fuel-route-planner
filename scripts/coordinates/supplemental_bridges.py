"""Join official Quarles numbered addresses to actual mapped fuel premises.

This is an offline identity bridge. It accepts one exact house/street match in
the CSV state, with agreement on the postal city or ZIP. No geocoded anchor is
returned as a verified station point.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from loves_ta import DATA, normalized, write_json
from loves_ta_conflicts import distance
from supplemental import street_key, targets

QUARLES_STATES = {
    "virginia": "VA",
    "north-carolina": "NC",
    "maryland": "MD",
    "pennsylvania": "PA",
    "delaware": "DE",
    "district-of-columbia": "DC",
    "west-virginia": "WV",
    "south-carolina": "SC",
    "tennessee": "TN",
    "texas": "TX",
    "new-mexico": "NM",
    "mississippi": "MS",
}


def fuel_records() -> list[dict]:
    records = []
    for path in sorted((DATA / "osm-states").glob("*.json")):
        if path.stem not in QUARLES_STATES:
            continue
        for record in json.loads(path.read_text(encoding="utf-8")).get("records", []):
            tags = record["tags"]
            if tags.get("amenity") != "fuel" or any(
                key.startswith(("disused:", "abandoned:", "demolished:")) for key in tags
            ):
                continue
            records.append({**record, "state": QUARLES_STATES[path.stem]})
    rows = {int(row["OPIS Truckstop ID"]): row for row in targets()}
    for path in sorted(DATA.glob("supplemental-quarles-map-*.json")):
        cached = json.loads(path.read_text(encoding="utf-8"))
        state = rows[cached["anchor"]["opis_id"]]["State"]
        for record in cached["records"]:
            tags = record["tags"]
            if tags.get("amenity") != "fuel" or any(
                key.startswith(("disused:", "abandoned:", "demolished:")) for key in tags
            ):
                continue
            records.append({**record, "state": state})
    return list({(record["type"], record["id"]): record for record in records}.values())


def quarles_bridges(records: list[dict]) -> tuple[list[dict], list[dict]]:
    guide = json.loads(
        (DATA / "supplemental-quarles-address-guide.json").read_text(encoding="utf-8")
    )
    candidates, unresolved = [], []
    by_address = defaultdict(list)
    for station in records:
        tags = station["tags"]
        if not tags.get("addr:housenumber") or not tags.get("addr:street"):
            continue
        actual = tags["addr:housenumber"] + " " + tags["addr:street"]
        by_address[(station["state"], street_key(actual))].append(station)
    anchor_path = DATA / "supplemental-quarles-census-anchors.json"
    anchors = (
        {
            row["opis_id"]: row
            for row in json.loads(anchor_path.read_text(encoding="utf-8"))
            if row["match_status"] == "Match"
        }
        if anchor_path.exists()
        else {}
    )
    for row in targets():
        if not normalized(row["Truckstop Name"]).startswith("QUARLES"):
            continue
        number = re.search(r"#\s*(\d+)", row["Truckstop Name"])
        identities = [
            identity
            for identity in guide
            if number
            and int(identity["store_number"]) == int(number[1])
            and (
                normalized(identity["city"]) == normalized(row["City"])
                or int(row["OPIS Truckstop ID"]) in anchors
                and normalized(row["City"])
                == normalized(
                    anchors[int(row["OPIS Truckstop ID"])]["matched_address"].split(",")[1]
                )
            )
        ]
        identities = {item["street_and_zip"]: item for item in identities}
        if len(identities) != 1:
            unresolved.append(
                {
                    "opis_id": int(row["OPIS Truckstop ID"]),
                    "reason": "No unique official numbered street identity",
                    "identities": list(identities.values()),
                }
            )
            continue
        identity = next(iter(identities.values()))
        match = re.fullmatch(r"(.*)\s+(\d{5})", identity["street_and_zip"])
        if not match:
            continue
        address, postcode = match.groups()
        valid = []
        for station in by_address[(row["State"], street_key(address))]:
            tags = station["tags"]
            city_agrees = normalized(tags.get("addr:city", "")) == normalized(identity["city"])
            zip_agrees = tags.get("addr:postcode", "")[:5] == postcode
            state_agrees = (
                station["state"] == row["State"]
                and tags.get("addr:state", row["State"]) == row["State"]
            )
            if state_agrees and (city_agrees or zip_agrees):
                valid.append(station)
        if len(valid) != 1:
            anchor = anchors.get(int(row["OPIS Truckstop ID"]))
            named = [
                station
                for station in records
                if anchor
                and station["state"] == row["State"]
                and "QUARLES"
                in normalized(
                    " ".join(
                        station["tags"].get(field, "") for field in ("name", "brand", "operator")
                    )
                )
                and abs(station["latitude"] - anchor["latitude"]) < 0.003
                and abs(station["longitude"] - anchor["longitude"]) < 0.003
                and distance(anchor, station) <= 150
            ]
            if len(named) == 1:
                valid = named
        if len(valid) != 1:
            unresolved.append(
                {
                    "opis_id": int(row["OPIS Truckstop ID"]),
                    "reason": "No unique exact-address mapped fuel premises",
                    "identity": identity,
                    "possible": valid,
                }
            )
            continue
        station = valid[0]
        url = f"https://www.openstreetmap.org/{station['type']}/{station['id']}"
        candidates.append(
            {
                "opis_id": int(row["OPIS Truckstop ID"]),
                "latitude": station["latitude"],
                "longitude": station["longitude"],
                "source": "official_numbered_address_on_mapped_fuel_premises",
                "source_url": url,
                "external_id": f"{station['type']}/{station['id']}",
                "store_number": number[1],
                "matched_name": row["Truckstop Name"],
                "matched_address": address,
                "identity_source_url": identity["source_url"],
                "csv_name": row["Truckstop Name"],
                "csv_address": row["Address"],
                "csv_city": row["City"],
                "csv_state": row["State"],
                "evidence": [
                    {
                        "source_url": identity["source_url"],
                        "reason": f"The current official October 2026 guide identifies Quarles #{number[1]} at {identity['street_and_zip']}, {identity['city']}. CSV number agrees; city is confirmed directly or by the documented street's Census postal city.",
                    },
                    {
                        "source_url": url,
                        "reason": "An actual mapped amenity=fuel has the identical normalized house number and street, with postal city or ZIP agreement; this mapped premises point is returned.",
                    },
                ],
            }
        )
        if street_key(
            station["tags"].get("addr:housenumber", "")
            + " "
            + station["tags"].get("addr:street", "")
        ) != street_key(address):
            candidates[-1]["evidence"][-1]["reason"] = (
                "An independently named Quarles amenity=fuel canopy is the unique named station within 150 meters of the Census street-search anchor for this official numbered address. The mapped fuel premises point, not the geocode, is returned."
            )
            candidates[-1]["evidence"].append(
                {
                    "source_url": anchors[int(row["OPIS Truckstop ID"])]["source_url"],
                    "reason": "Census result is retained solely as a search anchor for the official numbered street; named physical fuel premises provide coordinate corroboration.",
                }
            )
    return candidates, unresolved


def main() -> None:
    candidates, unresolved = quarles_bridges(fuel_records())
    candidates.extend(reviewed_properties())
    additional = DATA / "supplemental-other-premises-candidates.json"
    if additional.exists():
        candidates.extend(json.loads(additional.read_text(encoding="utf-8")))
    aerial = DATA / "supplemental-quarles-aerial-candidates.json"
    if aerial.exists():
        existing_ids = {row["opis_id"] for row in candidates}
        candidates.extend(
            row
            for row in json.loads(aerial.read_text(encoding="utf-8"))
            if row["opis_id"] not in existing_ids
        )
    accepted_ids = {row["opis_id"] for row in candidates}
    unresolved = [row for row in unresolved if row["opis_id"] not in accepted_ids]
    write_json(DATA / "supplemental-bridge-candidates.json", candidates)
    write_json(DATA / "supplemental-quarles-bridge-unresolved.json", unresolved)
    quarles_count = sum(row["csv_name"].upper().startswith("QUARLES") for row in candidates)
    print(
        {
            "quarles_mapped_addresses": quarles_count,
            "other_reviewed_premises": len(candidates) - quarles_count,
            "quarles_unresolved": len(unresolved),
        }
    )


def reviewed_properties() -> list[dict]:
    reviewed = {
        70939: (
            "way",
            491024341,
            "The independently named Quarles Fleet Fueling building has the exact 7801 Wellingford Drive address and Manassas postal city in the official numbered guide.",
        ),
        70940: (
            "node",
            1335402385,
            "The independently named Quarles Fuel building point explicitly identifies 9099 Owens Court, Manassas Park, and ZIP20111. The official guide and CSV use Manassas; Census confirms this exact street, 68 meters from the named premises.",
        ),
        70953: (
            "way",
            625745678,
            "The independently named Quarles Fleet Fueling Station property has the exact 4901 Massaponax Church Road address, Fredericksburg city and ZIP22407 in the current official guide. The mapped fueling-property point is used.",
        ),
        70954: (
            "way",
            610921590,
            "The independently named Quarles Fleet Fueling canopy is mapped as a shelter/building on Mechanical Boulevard, 59 meters from the official 1557 Mechanical Boulevard street anchor. The current guide explicitly identifies this numbered diesel/unleaded/DEF site; the canopy point is used.",
        ),
    }
    guide = json.loads(
        (DATA / "supplemental-quarles-address-guide.json").read_text(encoding="utf-8")
    )
    rows = {int(row["OPIS Truckstop ID"]): row for row in targets()}
    candidates = []
    for station_id, (osm_type, osm_id, reason) in reviewed.items():
        row = rows[station_id]
        number = re.search(r"#\s*(\d+)", row["Truckstop Name"])[1]
        identity = next(item for item in guide if int(item["store_number"]) == int(number))
        cached = json.loads(
            (DATA / f"supplemental-quarles-map-{station_id}.json").read_text(encoding="utf-8")
        )
        point = next(
            item for item in cached["records"] if item["type"] == osm_type and item["id"] == osm_id
        )
        url = f"https://www.openstreetmap.org/{osm_type}/{osm_id}"
        candidates.append(
            {
                "opis_id": station_id,
                "latitude": point["latitude"],
                "longitude": point["longitude"],
                "source": "manually_reviewed_station_premises",
                "source_url": url,
                "external_id": f"{osm_type}/{osm_id}",
                "store_number": number,
                "matched_name": row["Truckstop Name"],
                "matched_address": identity["street_and_zip"],
                "csv_name": row["Truckstop Name"],
                "csv_address": row["Address"],
                "csv_city": row["City"],
                "csv_state": row["State"],
                "coordinate_role": "fueling_premises",
                "identity_source_url": identity["source_url"],
                "evidence": [
                    {
                        "source_url": identity["source_url"],
                        "reason": f"Current official Quarles guide confirms #{number} at {identity['street_and_zip']}, {identity['city']}.",
                    },
                    {"source_url": url, "reason": reason},
                ],
            }
        )
    write_json(DATA / "supplemental-quarles-property-reviews.json", candidates)
    return candidates


if __name__ == "__main__":
    main()
