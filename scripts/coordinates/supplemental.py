"""Match selected CSV chains to cached official locator records.

Run offline after acquisition. Store identity, brand, state and postal city must
agree; Allsup's WordPress references are joined to its official store directory.
"""

from __future__ import annotations

import csv
import html
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from loves_ta import normalized, write_json

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "coordinate-sources"
SPIDERS = {
    "allsups_yesway_us": ("Allsup's", "Yesway"),
    "cefco_us": ("CEFCO",),
    "race_trac_us": ("RaceTrac",),
    "rutters_us": ("Rutter's",),
    "sheetz": ("Sheetz",),
}


def brand(name: str) -> str | None:
    value = normalized(name)
    for prefix, canonical in (
        ("ALLSUP", "Allsup's"),
        ("YESWAY", "Yesway"),
        ("QUARLES", "Quarles"),
        ("QUICKFUEL", "QuickFuel"),
        ("SHEETZ", "Sheetz"),
        ("RUTTER", "Rutter's"),
        ("CEFCO", "CEFCO"),
        ("RACETRAC", "RaceTrac"),
    ):
        if value.startswith(prefix):
            return canonical
    return None


def street_key(value: str) -> str:
    value = re.sub(r"\bP\.?\s*O\.?\s*BOX\s*\d+.*$", "", value, flags=re.IGNORECASE)
    replacements = {
        "STREET": "ST",
        "AVENUE": "AVE",
        "ROAD": "RD",
        "HIGHWAY": "HWY",
        "BOULEVARD": "BLVD",
        "DRIVE": "DR",
        "LANE": "LN",
        "COURT": "CT",
        "PARKWAY": "PKWY",
        "NORTH": "N",
        "SOUTH": "S",
        "EAST": "E",
        "WEST": "W",
    }
    value = value.upper()
    for before, after in replacements.items():
        value = re.sub(r"\b" + before + r"\b", after, value)
    return normalized(value)


def targets() -> list[dict]:
    path = DATA / "supplemental-target-rows.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    unresolved = {
        row["opis_id"] for row in json.loads((DATA / "unresolved.json").read_text(encoding="utf-8"))
    }
    rows = {}
    with (ROOT / "fuel-prices-for-be-assessment.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        for row in csv.DictReader(stream):
            station_id = int(row["OPIS Truckstop ID"])
            if station_id in unresolved and brand(row["Truckstop Name"]):
                rows.setdefault(station_id, row)
    write_json(path, list(rows.values()))
    return list(rows.values())


def atp_records() -> list[dict]:
    metadata = json.loads(
        (DATA / "supplemental-download-metadata.json").read_text(encoding="utf-8")
    )
    directory = json.loads(
        (DATA / "supplemental-yesway-store-numbers.json").read_text(encoding="utf-8")
    )
    official_numbers = defaultdict(list)
    for row in directory:
        if row["runtime_eligible"]:
            official_numbers[(row["brand"], street_key(row["address"]))].append(row)
    records = []
    government = government_identities()
    for spider in SPIDERS:
        source = json.loads((DATA / f"supplemental-atp-{spider}.txt").read_text(encoding="utf-8"))
        for feature in source["features"]:
            attributes = feature["properties"]
            if attributes.get("addr:country") != "US":
                continue
            record = {
                "brand": attributes["brand"],
                "external_id": str(attributes.get("ref", "")),
                "store_number": str(attributes.get("ref", "")),
                "latitude": feature["geometry"]["coordinates"][1],
                "longitude": feature["geometry"]["coordinates"][0],
                "address": attributes.get("addr:street_address", attributes.get("addr:full", "")),
                "city": attributes.get("addr:city", ""),
                "state": attributes.get("addr:state", ""),
                "source_url": attributes.get("website", attributes["@source_uri"]),
                "locator_source_url": attributes["@source_uri"],
                "dataset_url": metadata["atp-" + spider]["source_url"],
                "source": "official_locator_via_alltheplaces",
                "spider": spider,
            }
            if spider == "cefco_us":
                address = record["address"].split(",")
                if len(address) >= 3:
                    record["city"] = address[-2].strip()
                    record["address"] = ",".join(address[:-2]).strip()
            if spider == "allsups_yesway_us":
                record["store_number"] = ""
                numbers = official_numbers[(record["brand"], street_key(record["address"]))]
                if len(numbers) == 1:
                    record["store_number"] = numbers[0]["store_number"]
                    record["identity_source_url"] = numbers[0]["source_url"]
                else:
                    numbers = [
                        identity
                        for identity in government
                        if identity["brand"] == record["brand"]
                        and identity["state"] == record["state"]
                        and normalized(identity["city"]) == normalized(record["city"])
                        and street_key(identity["address"]) == street_key(record["address"])
                    ]
                    by_number = {identity["store_number"]: identity for identity in numbers}
                    if len(by_number) == 1:
                        identity = next(iter(by_number.values()))
                        record["store_number"] = identity["store_number"]
                        record["identity_source_url"] = identity["source_url"]
                        record["identity_kind"] = "government_retailer_number_and_address"
            records.append(record)
    return records


def government_identities() -> list[dict]:
    records = []
    for name in (
        "supplemental-usda-current-stations.json",
        "supplemental-usda-historical-stations.json",
    ):
        path = DATA / name
        if not path.exists():
            continue
        for row in json.loads(path.read_text(encoding="utf-8")):
            title = row.get("Store_Name", row.get("name", ""))
            number = re.search(r"\b(\d{1,6})\b", title)
            station_brand = brand(title)
            if not number or not station_brand:
                continue
            records.append(
                {
                    "brand": station_brand,
                    "store_number": number[1],
                    "address": row.get("Store_Street_Address", row.get("address", "")),
                    "city": row.get("City", row.get("city", "")),
                    "state": row.get("State", row.get("state", "")),
                    "source_url": row["source_url"],
                }
            )
    return records


def quickfuel_records() -> list[dict]:
    identity_path = DATA / "supplemental-quickfuel-identity-records.json"
    identities = (
        json.loads(identity_path.read_text(encoding="utf-8")) if identity_path.exists() else []
    )
    legacy_path = DATA / "supplemental-nrha-legacy-identity.json"
    legacy = []
    if legacy_path.exists():
        for row in json.loads(legacy_path.read_text(encoding="utf-8")):
            fields = row["fields"]
            number = re.search(r"#(\d+)", fields[0])
            if number and len(fields) >= 6:
                legacy.append(
                    {
                        "store_number": number[1],
                        "address": fields[1],
                        "city": fields[2],
                        "state": fields[4],
                        "source_url": "https://nrha.com/media/pdf/partner/nfuelloc.pdf",
                        "source_page": row["page"],
                    }
                )
    source = json.loads((DATA / "supplemental-quickfuel-official.json").read_text(encoding="utf-8"))
    records = []
    for feature in source["features"]:
        props = feature["properties"]
        address = {}
        for name in ("address-line1", "locality", "administrative-area", "postal-code"):
            match = re.search(r'class="' + name + r'">([^<]+)</span>', props["description"])
            address[name] = html.unescape(match[1]).strip() if match else ""
        record = {
            "brand": "QuickFuel",
            "external_id": str(feature["id"]),
            "store_number": "",
            "latitude": feature["geometry"]["coordinates"][1],
            "longitude": feature["geometry"]["coordinates"][0],
            "address": address["address-line1"],
            "city": address["locality"],
            "state": address["administrative-area"],
            "source_url": props["view_information"],
            "locator_source_url": "https://www.quickfuel.com/geofield-locator/json",
            "source": "quickfuel_official_locator",
        }
        matching = [
            identity
            for identity in identities
            if identity["state"] == record["state"]
            and street_key(identity["address"]) == street_key(record["address"])
        ]
        if not matching:
            matching = [
                identity
                for identity in legacy
                if identity["state"] == record["state"]
                and street_key(identity["address"]) == street_key(record["address"])
            ]
        if len(matching) == 1:
            record["store_number"] = matching[0]["store_number"]
            record["identity_source_url"] = matching[0]["source_url"]
            record["city_alias"] = matching[0]["city"]
            record["identity_kind"] = "fuel_card_network_number_and_address"
        records.append(record)
    return records


def former_cefco_records() -> list[dict]:
    historical_path = DATA / "supplemental-comdata-addresses.json"
    if not historical_path.exists():
        return []
    historical = json.loads(historical_path.read_text(encoding="utf-8"))
    historical.extend(
        {**identity, "name": "CEFCO #" + identity["store_number"]}
        for identity in government_identities()
        if identity["brand"] == "CEFCO"
    )
    current = json.loads((DATA / "regional-caseys-locations.json").read_text(encoding="utf-8"))
    records = []
    for old in historical:
        number = re.search(r"CEFCO\s+#?0*(\d+)", old["name"], flags=re.IGNORECASE)
        if not number:
            continue
        matching = [
            store
            for store in current
            if store["state"] == old["state"]
            and normalized(store["city"]) == normalized(old["city"])
            and street_key(store["address"]) == street_key(old["address"])
        ]
        if len(matching) != 1:
            continue
        store = matching[0]
        records.append(
            {
                **store,
                "brand": "CEFCO",
                "store_number": number[1],
                "matched_name": store["name"] + f" (formerly CEFCO #{number[1]})",
                "source": "official_current_premises_with_historical_identity",
                "identity_source_url": old["source_url"],
                "identity_kind": "cefco_to_caseys_same_address",
            }
        )
    unique = {(record["external_id"], record["store_number"]): record for record in records}
    return list(unique.values())


def main() -> None:
    rows = targets()
    records = atp_records() + quickfuel_records() + former_cefco_records()
    write_json(DATA / "supplemental-source-records.json", records)
    candidates, unresolved = [], []
    manual_path = DATA / "supplemental-manual-identities.json"
    manual = (
        {
            review["opis_id"]: review
            for review in json.loads(manual_path.read_text(encoding="utf-8"))
        }
        if manual_path.exists()
        else {}
    )
    for row in rows:
        station_brand = brand(row["Truckstop Name"])
        number = re.search(r"#\s*(\d+)", row["Truckstop Name"])
        matching = [
            record
            for record in records
            if number
            and record["store_number"]
            and int(number[1]) == int(record["store_number"])
            and record["brand"] == station_brand
            and record["state"] == row["State"].strip()
        ]
        valid = [
            record
            for record in matching
            if normalized(row["City"])
            in {normalized(record["city"]), normalized(record.get("city_alias", ""))}
        ]
        review = manual.get(int(row["OPIS Truckstop ID"]))
        if review:
            valid = [
                record
                for record in records
                if record["external_id"] == review["external_id"]
                and record["brand"] == review["brand"] == station_brand
                and record["state"] == review["state"] == row["State"]
                and record["address"] == review["official_address"]
            ]
            if len(valid) == 1:
                valid = [{**valid[0], "store_number": number[1]}]
        if len(valid) != 1:
            unresolved.append(
                {
                    **row,
                    "reason": "Store number, brand, state and postal city do not identify one official station",
                    "possible": matching,
                }
            )
            continue
        record = valid[0]
        evidence = [
            "CSV store number is confirmed by the locator or documented site identity",
            "CSV brand and state agree with the identity record",
            "Reviewed postal-city alias"
            if review
            else "Normalized postal city matches locator or documented address identity",
            "Coordinates are the point published by the official locator"
            if not (review and review.get("latitude"))
            else "Reviewed network coordinate corroborated by independently named fuel premises",
        ]
        if record.get("identity_source_url"):
            if record.get("identity_kind") == "cefco_to_caseys_same_address":
                evidence.append(
                    "Historical fuel-card directory confirms the CEFCO number and street address; current official Casey's locator identifies the same premises"
                )
            elif record.get("identity_kind") == "fuel_card_network_number_and_address":
                evidence.append(
                    "Fuel-card provider joins legacy store number to the exact street address in the current official locator"
                )
            elif record.get("identity_kind") == "government_retailer_number_and_address":
                evidence.append(
                    "USDA retailer identity joins number to exact street address in the current official locator"
                )
            else:
                evidence.append(
                    "Official professional-driver directory joins store number to exact street address"
                )
        candidates.append(
            {
                "opis_id": int(row["OPIS Truckstop ID"]),
                "latitude": record["latitude"],
                "longitude": record["longitude"],
                "source": record["source"],
                "source_url": record["source_url"],
                "external_id": record["external_id"],
                "store_number": record["store_number"],
                "matched_name": record.get(
                    "matched_name", f"{record['brand']} #{record['store_number']}"
                ),
                "matched_address": record["address"],
                "evidence": evidence,
                "dataset_url": record.get("dataset_url"),
                "identity_source_url": record.get("identity_source_url"),
                "csv_name": row["Truckstop Name"],
                "csv_address": row["Address"],
                "csv_city": row["City"].strip(),
                "csv_state": row["State"].strip(),
            }
        )
        if review:
            candidates[-1]["evidence"].extend(review["evidence"])
            for name in ("latitude", "longitude", "source", "source_url", "rejected_candidates"):
                if name in review:
                    candidates[-1][name] = review[name]
    bridge_path = DATA / "supplemental-bridge-candidates.json"
    if bridge_path.exists():
        bridges = json.loads(bridge_path.read_text(encoding="utf-8"))
        existing_ids = {candidate["opis_id"] for candidate in candidates}
        candidates.extend(row for row in bridges if row["opis_id"] not in existing_ids)
        bridged_ids = {row["opis_id"] for row in bridges}
        unresolved = [row for row in unresolved if int(row["OPIS Truckstop ID"]) not in bridged_ids]
    write_json(DATA / "supplemental-candidates.json", candidates)
    write_json(DATA / "supplemental-unresolved.json", unresolved)
    print(
        json.dumps(
            {
                "candidates": len(candidates),
                "unresolved": len(unresolved),
                "brands": Counter(brand(r["csv_name"]) for r in candidates),
            }
        )
    )


if __name__ == "__main__":
    main()
