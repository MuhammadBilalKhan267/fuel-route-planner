"""Cache Georgia retailer address leads; never publish their geocodes as fuel points."""

import argparse
import hashlib
import html
import json
import math
import re
import shutil
import time
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit

import httpx

from scripts.coordinates.exit_anchors import SOURCES, write_json
from scripts.coordinates.matching import brand, normalized, station_name_keys, store_number

ENDPOINT = "https://www.lotterymap.com/api/licensed-retailers"
MAX_BYTES = 512 * 1024
HEADROOM = 64 * 1024 * 1024
QUERIES = (
    "gas n go",
    "parker",
    "sunstop",
    "enmarket",
    "gas pro",
    "sprint",
    "friendly express",
    "el cheapo",
    "chapman",
    "forks",
)
GENERIC_BRANDS = {"bp", "shell", "chevron", "exxon", "marathon", "sunoco", "valero"}


def query_url(query):
    return (
        ENDPOINT
        + "?"
        + urlencode(
            {
                "state": "GA",
                "page": 1,
                "pageSize": 200,
                "sortBy": "name",
                "sortDir": "asc",
                "search": query,
            }
        )
    )


def cache_key(query):
    return "-".join(re.findall(r"[a-z0-9]+", query.lower()))


def retailer_records(value, source_url):
    """Keep exact public identities and label native geocodes as unverified leads."""
    if not isinstance(value, dict) or not isinstance(value.get("retailers"), list):
        raise TypeError("Retailer response is not a directory page")
    records = []
    for row in value["retailers"]:
        if not isinstance(row, dict) or row.get("state") != "GA":
            continue
        if not all(
            isinstance(row.get(key), str) and row[key].strip()
            for key in ("id", "name", "address", "city")
        ):
            continue
        record = {
            "external_id": "lotterymap:" + str(row["id"]),
            "name": row["name"],
            "street": row["address"],
            "city": row["city"],
            "state": "GA",
            "source_url": source_url,
            "source_role": "secondary_public_retailer_address_lead",
            "identity_verified": False,
            "coordinate_verified": False,
        }
        latitude, longitude = row.get("lat"), row.get("lng")
        if (
            isinstance(latitude, (int, float))
            and not isinstance(latitude, bool)
            and isinstance(longitude, (int, float))
            and not isinstance(longitude, bool)
            and math.isfinite(latitude)
            and math.isfinite(longitude)
            and 30 <= latitude <= 36
            and -86 <= longitude <= -80
        ):
            record["unverified_search_point"] = {"latitude": latitude, "longitude": longitude}
        records.append(record)
    return records


def download_query(query, directory=SOURCES, client=None):
    """One capped public GET, with durable failure evidence and no retries."""
    path = directory / f"regional-ga-lottery-{cache_key(query)}.json"
    url = query_url(query)
    if path.exists():
        return retailer_records(json.loads(path.read_text(encoding="utf-8")), url)
    attempt_path = path.with_name(path.stem + "-acquisition.json")
    attempt = {
        "source_url": url,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "max_bytes": MAX_BYTES,
        "automatic_retry": False,
        "status": "started",
    }
    # A failed attempt must be reviewed explicitly, never silently repeated.
    with attempt_path.open("x", encoding="utf-8") as handle:
        json.dump(attempt, handle, indent=2)
    owned = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(35, connect=15), follow_redirects=False)
    try:
        if shutil.disk_usage(directory).free < MAX_BYTES + HEADROOM:
            raise ValueError("Insufficient free space for bounded source acquisition")
        with client.stream("GET", url) as response:
            attempt["http_status"] = response.status_code
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > MAX_BYTES:
                    raise ValueError("Retailer response exceeds download limit")
            value = json.loads(content)
            records = retailer_records(value, url)
            write_json(path, value)
            attempt.update(
                status="saved",
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                page=value.get("page"),
                total_pages=value.get("totalPages"),
                returned_records=len(records),
            )
            return records
    except Exception as exc:
        attempt.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        write_json(attempt_path, attempt)
        if owned:
            client.close()


def identity_lead(row, record):
    """An exact name/number produces a review lead, never station acceptance."""
    if row["state"] != record["state"] or normalized(row["city"]) != normalized(record["city"]):
        return None
    for name in row["names"]:
        expected, actual = store_number(name), store_number(record["name"])
        if expected and actual and expected != actual:
            continue
        chain = brand(name)
        if chain and chain == brand(record["name"]):
            if expected and expected == actual:
                return "same_numbered_brand_city_state_address_lead"
            if chain in GENERIC_BRANDS:
                continue
        keys = station_name_keys(name) & station_name_keys(record["name"])
        if any(len(key) >= 7 for key in keys):
            return "same_distinctive_name_city_state_address_lead"
    return None


def friendly_express_points(text):
    """Parse the current operator feed, including its empty terminal object."""
    value = json.loads(text, strict=False)
    if not isinstance(value, dict) or not isinstance(value.get("locations"), list):
        raise TypeError("Friendly Express response is not a location list")
    records = []
    for row in value["locations"]:
        if not row or row.get("state") != "GA":
            continue
        number = store_number(row.get("title", ""))
        point = row.get("coordinates", [])
        street = html.unescape(re.sub(r"<[^>]+>", "", row.get("address", ""))).strip()
        if (
            not number
            or row.get("url_title") != "store-" + number
            or not street
            or not row.get("city")
            or len(point) != 2
        ):
            continue
        try:
            latitude, longitude = map(float, point)
        except (ValueError, TypeError):
            continue
        if not (30 <= latitude <= 36 and -86 <= longitude <= -80):
            continue
        records.append(
            {
                "external_id": "friendly-express:" + number,
                "name": "Friendly Express #" + number,
                "street": street,
                "city": row["city"],
                "state": "GA",
                "source_url": "https://friendlyexpress.com/locations/" + row["url_title"],
                "feed_url": "https://friendlyexpress.com/locations/search",
                "native_search_point": {"latitude": latitude, "longitude": longitude},
                "coordinate_verified": False,
            }
        )
    return records


def sunstop_points(value):
    """Keep numbered operator identities; their geocodes still require review."""
    if not isinstance(value, list):
        raise TypeError("SunStop response is not a location list")
    records = []
    for row in value:
        if not isinstance(row, dict) or row.get("state") != "GA":
            continue
        number = store_number(row.get("store", ""))
        expected_url = f"https://sunstopstores.com/stores/sunstop-{number}/"
        if (
            not number
            or row.get("permalink") != expected_url
            or not row.get("address")
            or not row.get("city")
        ):
            continue
        try:
            latitude, longitude = float(row["lat"]), float(row["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (30 <= latitude <= 36 and -86 <= longitude <= -80):
            continue
        records.append(
            {
                "external_id": f"sunstop:{number}",
                "name": row["store"],
                "street": row["address"],
                "city": row["city"],
                "state": "GA",
                "source_url": expected_url,
                "native_search_point": {"latitude": latitude, "longitude": longitude},
                "coordinate_verified": False,
            }
        )
    return records


def map_destination_point(url):
    """Read an explicit place coordinate, never the map's viewport centre."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.google.com"
        or not parsed.path.startswith("/maps/place/")
        or parsed.username
    ):
        return None
    points = set(re.findall(r"!8m2!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)", url))
    if len(points) != 1:
        return None
    latitude, longitude = map(float, points.pop())
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return {"latitude": latitude, "longitude": longitude}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    records = {}
    for query in QUERIES:
        if args.download:
            rows = download_query(query)
            time.sleep(1)
        else:
            path = SOURCES / f"regional-ga-lottery-{cache_key(query)}.json"
            if not path.exists():
                continue
            rows = retailer_records(json.loads(path.read_text(encoding="utf-8")), query_url(query))
        records.update({row["external_id"]: row for row in rows})
    unresolved = json.loads((SOURCES / "unresolved.json").read_text(encoding="utf-8"))
    proposals = []
    for row in unresolved:
        if row["state"] != "GA":
            continue
        for record in records.values():
            rule = identity_lead(row, record)
            if rule:
                proposals.append(
                    {"opis_id": row["opis_id"], "original": row, "rule": rule, "lead": record}
                )
    write_json(SOURCES / "regional-ga-retailer-address-leads.json", proposals)
    friendly = SOURCES / "regional-ga-friendly-primary.txt"
    if friendly.exists():
        write_json(
            SOURCES / "regional-ga-friendly-primary-points.json",
            friendly_express_points(friendly.read_text(encoding="utf-8")),
        )
    sunstop = SOURCES / "regional-ga-sunstop-markers.json"
    if sunstop.exists():
        write_json(
            SOURCES / "regional-ga-sunstop-primary-points.json",
            sunstop_points(json.loads(sunstop.read_text(encoding="utf-8"))),
        )
    print(
        f"{len(records)} distinct public retailer leads; {len(proposals)} unresolved identity proposals"
    )


if __name__ == "__main__":
    main()
