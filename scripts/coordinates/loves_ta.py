"""Resolve CSV stations against the chains' public location directories.

Downloads are cached and serial, with at most one request per second per host.
This preparation script does not use the routing account or alter the database.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "coordinate-sources"
CSV = ROOT / "fuel-prices-for-be-assessment.csv"
LOVES_FEED = "https://www.loves.com/api/fetch_stores"
LOVES_DIRECTORY = "https://www.loves.com/location-and-fuel-price-search/all-locations"
TA_FEED = "https://www.ta-petro.com/fleet-map/"
US_STATES = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "DC",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
}


def write_json(path: Path, data: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(5):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            # Windows readers can briefly hold the destination during replacement.
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def normalized(value: str) -> str:
    value = value.upper().replace("SAINT", "ST").replace("MOUNT", "MT").replace("FORT", "FT")
    value = re.sub(r"\bNORTH\b", "N", value)
    return re.sub(r"[^A-Z0-9]", "", value)


def highways(value: str) -> set[str]:
    routes = set()
    for match in re.finditer(
        r"\b(I|US|SR)(?:\s+(?:HWY|HIGHWAY))?[ -]*(\d+)((?:/\d+)*)", value.upper()
    ):
        routes.add(match[1] + match[2])
        routes.update(match[1] + number for number in match[3].split("/") if number)
    return routes


def exits(value: str) -> set[str]:
    return {
        number
        for group in re.findall(r"\bEXIT\s*(\d+(?:/\d+)*)", value.upper())
        for number in group.split("/")
    }


def choose_station(row: dict, options: list[dict]) -> dict | None:
    names = [
        store
        for store in options
        if normalized(store["name"].split("#")[0]) in normalized(row["Truckstop Name"])
    ]
    if len(names) == 1:
        return names[0]
    city = [store for store in options if normalized(store["city"]) == normalized(row["City"])]
    number = re.search(r"#\s*(\d+)", row["Truckstop Name"])
    identities = [
        store
        for store in options
        if number
        and store["external_id"]
        and int(number[1]) == int(store["external_id"])
        and (
            store in city
            or highways(row["Address"]) & highways(store["description"])
            and exits(row["Address"]) & exits(store["description"])
        )
    ]
    if len(identities) == 1:
        return identities[0]
    road_city = [
        store
        for store in city
        if highways(row["Address"]) & highways(store["description"])
        and exits(row["Address"]) & exits(store["description"])
    ]
    if len(road_city) == 1:
        return road_city[0]
    postal_city = (
        city
        if len(city) == 1
        else [store for store in options if normalized(row["City"]) in normalized(store["name"])]
    )
    if len(postal_city) != 1:
        return None
    station = postal_city[0]
    if (
        exits(row["Address"])
        and exits(station["description"])
        and not (exits(row["Address"]) & exits(station["description"]))
    ):
        return None
    return station


def json_ld(page: str) -> dict:
    scripts = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page,
        re.DOTALL | re.IGNORECASE,
    )
    for script in scripts:
        value = json.loads(script)
        for record in value if isinstance(value, list) else [value]:
            if isinstance(record, dict) and record.get("geo"):
                return record
    raise ValueError("No station coordinates in JSON-LD")


class PublicDownloads:
    def __init__(self) -> None:
        self.last: dict[str, float] = {}
        self.requests = 0
        self.client = httpx.Client(
            timeout=60, follow_redirects=True, event_hooks={"request": [self.throttle]}
        )

    def throttle(self, request: httpx.Request) -> None:
        host = request.url.host
        remaining = self.last.get(host, 0) + 1 - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        self.last[host] = time.monotonic()
        self.requests += 1

    def get(self, url: str) -> str:
        host = urlsplit(url).hostname or ""
        response = self.client.get(url)
        if response.status_code in {401, 403, 429}:
            raise RuntimeError(f"Stopped public downloads: {response.status_code} at {host}")
        response.raise_for_status()
        return response.text


def load_loves(downloads: PublicDownloads, online: bool) -> list[dict]:
    path = DATA / "loves-official-records.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    raw = DATA / "loves-fetch-stores.json"
    if raw.exists():
        stores = json.loads(raw.read_text(encoding="utf-8"))["stores"]
    elif online:
        stores = json.loads(downloads.get(LOVES_FEED))["stores"]
    else:
        raise RuntimeError("Love's feed is missing; run with --download")
    fields = (
        "number",
        "latitude",
        "longitude",
        "address1",
        "city",
        "state",
        "zip",
        "exitNumber",
        "highway",
        "preferredName",
        "facilitySubtypeName",
        "isLoveStore",
    )
    records = [{key: store.get(key) for key in fields} for store in stores]
    write_json(path, records)
    return records


def load_ta(downloads: PublicDownloads, online: bool) -> list[dict]:
    path = DATA / "ta-official-records.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    raw = DATA / "ta-fleet.html"
    if raw.exists():
        page = raw.read_text(encoding="utf-8")
    elif online:
        page = downloads.get(TA_FEED)
    else:
        raise RuntimeError("TA feed is missing; run with --download")
    match = re.search(r"const taLocationMapMarkers\s*=\s*(\[.*?\]);", page, re.DOTALL)
    if match is None:
        raise ValueError("Official TA station markers are absent")
    records = json.loads(match[1])
    write_json(path, records)
    return records


def load_loves_links(downloads: PublicDownloads, online: bool) -> dict[str, str]:
    path = DATA / "loves-official-links.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    raw = DATA / "loves-directory.html"
    if raw.exists():
        page = raw.read_text(encoding="utf-8")
    elif online:
        page = downloads.get(LOVES_DIRECTORY)
    else:
        return {}
    links = {}
    for link in re.findall(r'href="([^"]+/loves-[^"]+)"', page):
        number = re.search(r"-(\d+)$", link)
        if number:
            links[number[1]] = "https://www.loves.com" + link
    write_json(path, links)
    return links


def station_rows() -> list[dict]:
    unique = {}
    with CSV.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["State"].strip() in US_STATES:
                unique.setdefault(row["OPIS Truckstop ID"], row)
    return list(unique.values())


def detail_records(
    downloads: PublicDownloads,
    urls: list[str],
    path: Path,
    online: bool,
) -> dict[str, dict]:
    records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if online:
        for url in urls:
            if url in records:
                continue
            try:
                page = downloads.get(url)
                station = json_ld(page)
            except ValueError as error:
                records[url] = {"error": str(error)}
            except httpx.HTTPStatusError as error:
                records[url] = {"error": f"Public page returned {error.response.status_code}"}
            else:
                address = station.get("address", {})
                geo = station["geo"]
                opis = re.search(r'"fieldName":"OPIS","fieldValue":"(\d+)"', page)
                records[url] = {
                    "name": station.get("name", ""),
                    "latitude": float(geo["latitude"]),
                    "longitude": float(geo["longitude"]),
                    "street_address": address.get("streetAddress", ""),
                    "city": address.get("addressLocality", ""),
                    "state": address.get("addressRegion", ""),
                    "description": station.get("description", ""),
                    "opis_id": str(int(opis[1])) if opis else "",
                }
            write_json(path, records)
            print(f"Details {len(records)}/{len(urls)}: {url}", flush=True)
    return records


def candidate(row: dict, station: dict, source: str, url: str, evidence: list[str]) -> dict:
    return {
        "opis_id": int(row["OPIS Truckstop ID"]),
        "latitude": station["latitude"],
        "longitude": station["longitude"],
        "source": source,
        "source_url": url,
        "external_id": str(station.get("number", station.get("external_id", ""))),
        "matched_name": station.get("name", "Love's " + str(station.get("number", ""))),
        "matched_address": station.get("street_address", station.get("address1", "")),
        "evidence": evidence,
        "csv_name": row["Truckstop Name"],
        "csv_address": row["Address"],
        "csv_city": row["City"].strip(),
        "csv_state": row["State"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Fetch missing public sources")
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    downloads = PublicDownloads()
    rows = station_rows()
    loves = [row for row in rows if re.match(r"^LOVE.?S", row["Truckstop Name"], re.IGNORECASE)]
    stores = {
        record["number"]: record
        for record in load_loves(downloads, args.download)
        if record["isLoveStore"]
    }
    links = load_loves_links(downloads, args.download)
    love_detail_urls = []
    for row in loves:
        number = re.search(r"#\s*(\d+)", row["Truckstop Name"])
        if number is None:
            continue
        store = stores.get(int(number[1]))
        if (store is None or normalized(store["city"]) != normalized(row["City"])) and number[
            1
        ] in links:
            love_detail_urls.append(links[number[1]])
    love_details = detail_records(
        downloads, love_detail_urls, DATA / "loves-official-details.json", args.download
    )
    review_path = DATA / "loves-manual-reviews.json"
    reviews = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {}
    accepted, unresolved = [], []
    for row in loves:
        number = re.search(r"#\s*(\d+)", row["Truckstop Name"])
        store = stores.get(int(number[1])) if number else None
        url = links.get(number[1], LOVES_FEED) if number else LOVES_FEED
        detail = love_details.get(url, {})
        if store is None or store["state"] != row["State"]:
            unresolved.append(
                {
                    **row,
                    "reason": "Store absent from current official feed or state conflict",
                    "manual_review": reviews.get(row["OPIS Truckstop ID"]),
                }
            )
            continue
        evidence = ["Official Love's store number matches CSV", "State matches"]
        if normalized(store["city"]) == normalized(row["City"]):
            evidence.append("Normalized city matches")
        elif detail.get("opis_id") == row["OPIS Truckstop ID"]:
            evidence.extend(
                [
                    "Official station page OPIS ID matches CSV",
                    "Postal city differs from CSV; identity confirmed by OPIS ID",
                ]
            )
        else:
            unresolved.append(
                {
                    **row,
                    "reason": "City differs; official OPIS confirmation unavailable",
                    "official": store,
                }
            )
            continue
        evidence.append(f"Official highway/exit: {store['highway']} / {store['exitNumber']}")
        accepted.append(
            candidate(
                row,
                {**store, "name": f"Love's #{store['number']}"},
                "loves_official_store",
                url,
                evidence,
            )
        )
    write_json(DATA / "loves-candidates.json", accepted)
    write_json(DATA / "loves-unresolved.json", unresolved)

    ta_rows = [
        row
        for row in rows
        if re.match(r"^(TA(?:\s|$)|PETRO(?:\s|$))", row["Truckstop Name"], re.IGNORECASE)
    ]
    markers = load_ta(downloads, args.download)
    detail_urls = ["https://www.ta-petro.com" + marker["locationLink"] for marker in markers]
    details = detail_records(
        downloads, detail_urls, DATA / "ta-official-details.json", args.download
    )
    review_path = DATA / "ta-manual-reviews.json"
    manual_reviews = (
        json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {}
    )
    ta_accepted, ta_unresolved = [], []
    by_state = defaultdict(list)
    for marker in markers:
        url = "https://www.ta-petro.com" + marker["locationLink"]
        detail = details.get(url)
        if not detail or "error" in detail:
            continue
        detail = {
            **detail,
            "source_url": url,
            "external_id": re.search(r"#(\d+)", detail["name"])[1]
            if re.search(r"#(\d+)", detail["name"])
            else "",
        }
        by_state[detail["state"]].append(detail)
    for row in ta_rows:
        brand = row["Truckstop Name"].split()[0].upper()
        options = [
            store
            for store in by_state[row["State"]]
            if store["name"].split()[0].upper() == brand
            and (
                brand == "TA"
                or "STOPPING CENTER" in row["Truckstop Name"].upper()
                or re.match(r"PETRO STOP\s+#", row["Truckstop Name"].upper())
                or normalized(store["name"].split("#")[0]) in normalized(row["Truckstop Name"])
            )
        ]
        number = re.search(r"#\s*(\d+)", row["Truckstop Name"])
        selected = choose_station(row, options)
        review = manual_reviews.get(row["OPIS Truckstop ID"], {})
        if review.get("source_url"):
            selected = next(
                (
                    store
                    for store in by_state[row["State"]]
                    if store["source_url"] == review["source_url"]
                ),
                None,
            )
        if review.get("runtime_eligible") is False:
            selected = None
        if selected is None:
            ta_unresolved.append(
                {
                    **row,
                    "reason": "No unambiguous official brand/city/name match",
                    "possible": options,
                    "manual_review": review,
                }
            )
            continue
        evidence = ["State matches"]
        if review:
            evidence.extend(review.get("evidence", []))
        else:
            evidence.append("Official TA/Petro brand matches")
        if normalized(selected["city"]) == normalized(row["City"]):
            evidence.append("Normalized city matches")
        else:
            evidence.append(
                "Official location name or station number with highway/exit matches; postal city differs"
            )
        if number and selected["external_id"] and int(number[1]) == int(selected["external_id"]):
            evidence.append("Official TA/Petro station number matches CSV")
        if highways(row["Address"]) & highways(selected["description"]):
            evidence.append("Highway agrees with CSV")
        if exits(row["Address"]) & exits(selected["description"]):
            evidence.append("Exit number agrees with CSV")
        evidence.append(selected["description"])
        ta_accepted.append(
            candidate(row, selected, "ta_petro_official_store", selected["source_url"], evidence)
        )
    write_json(DATA / "ta-candidates.json", ta_accepted)
    write_json(DATA / "ta-unresolved.json", ta_unresolved)
    print(
        json.dumps(
            {
                "loves_total": len(loves),
                "loves_accepted": len(accepted),
                "loves_unresolved": len(unresolved),
                "ta_total": len(ta_rows),
                "ta_accepted": len(ta_accepted),
                "ta_unresolved": len(ta_unresolved),
                "public_get_requests": downloads.requests,
            }
        )
    )
    downloads.client.close()


if __name__ == "__main__":
    main()
