"""Recover exact interstate/exit search anchors; fuel coordinates need separate proof."""

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import tarfile
import time
from collections import defaultdict
from datetime import UTC, datetime

import httpx
from shapely import STRtree, make_valid, prepare
from shapely.geometry import Point, box, shape

from scripts.coordinates.match_osm import GEOD, ROOT, SOURCES, load_pois, stations
from scripts.coordinates.matching import brand, normalized, station_name_keys, store_number

ENDPOINT = "https://overpass-api.de/api/interpreter"
SERVER_SECONDS = 90
SERVER_MEMORY = 128 * 1024 * 1024
MAX_DOWNLOAD = 64 * 1024 * 1024
PRIOR_COMPUTE_SECONDS = 390
QUERY = f"""[out:json][timeout:{SERVER_SECONDS}][maxsize:{SERVER_MEMORY}];
node[\"highway\"=\"motorway_junction\"][\"ref\"](18,-180,72,-65)->.exits;
way(bn.exits)[\"highway\"=\"motorway\"]->.roads;
(.exits;.roads;);
out body;
"""
DOCUMENTATION = [
    "https://wiki.openstreetmap.org/wiki/Overpass_API",
    "https://dev.overpass-api.de/overpass-doc/en/preface/commons.html",
    "https://wiki.openstreetmap.org/wiki/Tag:highway=motorway_junction",
]


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_snapshot(value):
    if not isinstance(value, dict) or not isinstance(value.get("elements"), list):
        raise TypeError("Overpass response is not an element snapshot")
    if value.get("remark"):
        raise ValueError("Overpass reported an incomplete or failed query")
    return value


def download_snapshot(directory, client=None, clock=time.monotonic):
    """One bounded attempt. A failure remains recorded and never triggers a retry."""
    snapshot = directory / "regional-exit-snapshot.json"
    if snapshot.exists():
        return validate_snapshot(json.loads(snapshot.read_text(encoding="utf-8")))
    if PRIOR_COMPUTE_SECONDS + SERVER_SECONDS >= 600:
        raise ValueError("Conservative total compute reservation reaches ten minutes")
    attempt_path = directory / "regional-exit-acquisition.json"
    attempt = {
        "source_url": ENDPOINT,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "query": QUERY,
        "server_timeout_seconds": SERVER_SECONDS,
        "server_max_memory_bytes": SERVER_MEMORY,
        "download_limit_bytes": MAX_DOWNLOAD,
        "prior_compute_reservation_seconds": PRIOR_COMPUTE_SECONDS,
        "status": "started",
        "documentation": DOCUMENTATION,
    }
    # Exclusive creation also prevents concurrent executions of this acquisition.
    with attempt_path.open("x", encoding="utf-8") as handle:
        json.dump(attempt, handle, indent=2)
    owns_client = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(SERVER_SECONDS + 20, connect=15),
        follow_redirects=False,
        headers={
            "User-Agent": "SpotterAssessment-ExitRecovery/1.0 (one-off cached OSM research)",
            "Accept": "application/json",
        },
    )
    start = clock()
    downloaded = 0
    try:
        chunks = []
        with client.stream("GET", ENDPOINT, params={"data": QUERY}) as response:
            attempt["http_status"] = response.status_code
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and int(declared) > MAX_DOWNLOAD:
                raise ValueError("Overpass response exceeds the download budget")
            for chunk in response.iter_bytes(chunk_size=65_536):
                downloaded += len(chunk)
                if downloaded > MAX_DOWNLOAD:
                    raise ValueError("Overpass response exceeds the download budget")
                if clock() - start > SERVER_SECONDS + 20:
                    raise TimeoutError("Overpass acquisition exceeded its elapsed-time budget")
                chunks.append(chunk)
        raw_value = json.loads(b"".join(chunks))
        if isinstance(raw_value, dict) and raw_value.get("remark"):
            attempt["server_remark"] = str(raw_value["remark"])[:2_000]
            write_json(directory / "regional-exit-failed-response.json", raw_value)
        value = validate_snapshot(raw_value)
        write_json(snapshot, value)
        attempt["status"] = "complete"
        return value
    except Exception as error:
        attempt["status"] = "failed"
        attempt["error_type"] = type(error).__name__
        raise
    finally:
        attempt["downloaded_bytes"] = downloaded
        attempt["elapsed_seconds"] = round(clock() - start, 3)
        write_json(attempt_path, attempt)
        if owns_client:
            client.close()


def route_refs(value):
    """Read explicit interstate refs, preserving H-series and three-digit routes."""
    result = set()
    for match in re.finditer(
        r"\b(?:I|INTERSTATE)\s*-?\s*(H(?:\s*-?\s*)?)?(\d{1,3})([A-Z]?)(?![A-Z0-9])",
        value,
        re.IGNORECASE,
    ):
        if re.match(r"\s+(?:BUS(?:INESS)?|ALT(?:ERNATE)?)\b", value[match.end() :], re.IGNORECASE):
            continue
        prefix = "H" if match[1] else ""
        result.add("I-" + prefix + str(int(match[2])) + match[3].upper())
    return result


def exit_ref(value):
    match = re.fullmatch(r"\s*(\d{1,4})(?:\s*-?\s*([A-Z]))?\s*", value, re.IGNORECASE)
    return str(int(match[1])) + (match[2] or "").upper() if match else None


def csv_exit_request(address):
    """Only one explicit EXIT token; ranges and old/new numbering stay ambiguous."""
    tokens = list(re.finditer(r"\bEXIT(?:S)?\b", address, re.IGNORECASE))
    if len(tokens) != 1 or tokens[0][0].upper() != "EXIT":
        return None
    before, after = address[: tokens[0].start()], address[tokens[0].end() :]
    match = re.match(r"\s*(\d{1,4}(?:\s*-?\s*[A-Z])?)(?![A-Z0-9])", after, re.IGNORECASE)
    if not match:
        return None
    remainder = after[match.end() :].lstrip()
    if remainder.startswith(("/", "-", ";")):
        return None
    if re.match(r"(?:WB|EB|NB|SB)\s*/\s*\d", remainder, re.IGNORECASE):
        return None
    routes = route_refs(before)
    ref = exit_ref(match[1])
    return {"routes": sorted(routes), "exit_ref": ref} if routes and ref else None


def state_boundaries():
    features = json.loads((ROOT / "trips/data/us-states.geojson").read_text())["features"]
    result = {
        item["properties"]["STUSAB"]: make_valid(shape(item["geometry"])) for item in features
    }
    for boundary in result.values():
        prepare(boundary)
    return result


def extract_anchors(snapshot, boundaries):
    nodes = {
        element["id"]: element
        for element in validate_snapshot(snapshot)["elements"]
        if element.get("type") == "node"
        and element.get("tags", {}).get("highway") == "motorway_junction"
    }
    parents = defaultdict(list)
    for way in snapshot["elements"]:
        if way.get("type") != "way" or way.get("tags", {}).get("highway") != "motorway":
            continue
        routes = route_refs(way["tags"].get("ref", ""))
        if not routes:
            continue
        for identifier in set(way.get("nodes", [])) & nodes.keys():
            parents[identifier].append({"way_id": way["id"], "routes": sorted(routes)})
    anchors = []
    for identifier, node in nodes.items():
        longitude, latitude = node.get("lon"), node.get("lat")
        if not isinstance(longitude, (int, float)) or not isinstance(latitude, (int, float)):
            continue
        if not math.isfinite(longitude) or not math.isfinite(latitude):
            continue
        states = [
            state
            for state, boundary in boundaries.items()
            if boundary.covers(Point(longitude, latitude))
        ]
        refs = {exit_ref(value) for value in node["tags"].get("ref", "").split(";")} - {None}
        for state in states:
            for ref in sorted(refs):
                for parent in parents[identifier]:
                    for route in parent["routes"]:
                        anchors.append(
                            {
                                "state": state,
                                "route": route,
                                "exit_ref": ref,
                                "latitude": latitude,
                                "longitude": longitude,
                                "junction_id": identifier,
                                "motorway_id": parent["way_id"],
                                "motorway_routes": parent["routes"],
                                "junction_tags": node["tags"],
                                "coordinate_verified": False,
                                "purpose": "exit_search_anchor_only",
                                "source_url": f"https://www.openstreetmap.org/node/{identifier}",
                                "motorway_source_url": f"https://www.openstreetmap.org/way/{parent['way_id']}",
                            }
                        )
    return anchors


def published_release(directory, client=None):
    """Use the publisher's checksummed release, avoiding another Overpass query."""
    metadata = json.loads((directory / "regional-exit-openinterstate-release.json").read_text())
    assets = [asset for asset in metadata["assets"] if asset["name"].endswith(".tar.gz")]
    if len(assets) != 1:
        raise ValueError("Expected one published interstate release archive")
    asset = assets[0]
    digest = asset.get("digest", "")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest) or not 0 < asset["size"] <= MAX_DOWNLOAD:
        raise ValueError("Published release has no valid checksum or exceeds the byte budget")
    archive = directory / "regional-exit-openinterstate.tar.gz"
    audit_path = directory / "regional-exit-published-acquisition.json"
    if not archive.exists():
        if shutil.disk_usage(directory).free < asset["size"] + 512 * 1024 * 1024:
            raise ValueError("Insufficient free space for bounded exit acquisition")
        audit = {
            "source_url": asset["browser_download_url"],
            "release_tag": metadata["tag_name"],
            "retrieved_at": datetime.now(UTC).isoformat(),
            "expected_bytes": asset["size"],
            "sha256": digest.removeprefix("sha256:"),
            "status": "started",
        }
        with audit_path.open("x", encoding="utf-8") as handle:
            json.dump(audit, handle, indent=2)
        owns_client = client is None
        client = client or httpx.Client(
            timeout=60,
            follow_redirects=True,
            headers={"User-Agent": "SpotterAssessment-ExitRecovery/1.0"},
        )
        start, downloaded = time.monotonic(), 0
        try:
            chunks = []
            with client.stream("GET", asset["browser_download_url"]) as response:
                audit["http_status"] = response.status_code
                response.raise_for_status()
                for chunk in response.iter_bytes(chunk_size=65_536):
                    downloaded += len(chunk)
                    if downloaded > asset["size"] or time.monotonic() - start > 120:
                        raise ValueError("Published archive exceeds its byte/time budget")
                    chunks.append(chunk)
            content = b"".join(chunks)
            if (
                len(content) != asset["size"]
                or hashlib.sha256(content).hexdigest() != audit["sha256"]
            ):
                raise ValueError("Published release checksum or size mismatch")
            archive.write_bytes(content)
            audit["status"] = "complete"
        except Exception as error:
            audit["status"], audit["error_type"] = "failed", type(error).__name__
            raise
        finally:
            audit["downloaded_bytes"] = downloaded
            write_json(audit_path, audit)
            if owns_client:
                client.close()
    with archive.open("rb") as handle:
        actual_digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if archive.stat().st_size != asset["size"] or actual_digest != digest.removeprefix("sha256:"):
        raise ValueError("Cached published archive fails checksum verification")
    wanted = {
        "corridor_exits.csv": "regional-exit-published-exits.csv",
        "places.csv": "regional-exit-published-places.csv",
        "source_lineage.json": "regional-exit-published-lineage.json",
        "manifest.json": "regional-exit-published-manifest.json",
    }
    extracted, scanned = set(), 0
    with tarfile.open(archive, "r|gz") as handle:
        for member in handle:
            scanned += member.size
            if scanned > 768 * 1024 * 1024:
                raise ValueError("Published archive exceeds the extraction scan allowance")
            name = member.name.rsplit("/", 1)[-1]
            if name not in wanted:
                continue
            if not member.isfile() or member.size > 48 * 1024 * 1024 or name in extracted:
                raise ValueError("Unexpected published archive member")
            stream = handle.extractfile(member)
            with stream:
                (directory / wanted[name]).write_bytes(stream.read())
            extracted.add(name)
            if extracted == wanted.keys():
                break
    if "corridor_exits.csv" not in extracted:
        raise ValueError("Published archive contains no exit table")
    return asset["browser_download_url"]


def published_anchors(path, boundaries, source_url):
    anchors = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            routes = route_refs(row["interstate_name"])
            if len(routes) != 1 or not re.fullmatch(r"node/\d+", row["exit_id"]):
                continue
            try:
                longitude, latitude = float(row["lon"]), float(row["lat"])
            except ValueError:
                continue
            if not math.isfinite(longitude) or not math.isfinite(latitude):
                continue
            refs = {exit_ref(value) for value in row["exit_number"].split(";")} - {None}
            for state, boundary in boundaries.items():
                if not boundary.covers(Point(longitude, latitude)):
                    continue
                for ref in sorted(refs):
                    anchors.append(
                        {
                            "state": state,
                            "route": next(iter(routes)),
                            "exit_ref": ref,
                            "latitude": latitude,
                            "longitude": longitude,
                            "junction_id": int(row["exit_id"].split("/")[1]),
                            "motorway_id": None,
                            "corridor_id": row["corridor_id"],
                            "direction": row["direction_code"],
                            "coordinate_verified": False,
                            "purpose": "exit_search_anchor_only",
                            "source_url": "https://www.openstreetmap.org/" + row["exit_id"],
                            "dataset_url": source_url,
                            "route_binding": "publisher_derived_interstate_corridor_requires_review",
                        }
                    )
    return anchors


def strict_identity(row, point):
    """A generic pump brand and proximity never establish a numbered station."""
    tags = point["tags"]
    actual_names = [tags.get(key, "") for key in ("name", "official_name", "alt_name")]
    wanted_numbers = {store_number(name) for name in row["names"]} - {None}
    actual_numbers = {store_number(name) for name in actual_names} - {None}
    for key in ("ref", "ref:store", "brand:ref"):
        if tags.get(key, "").isdecimal():
            actual_numbers.add(str(int(tags[key])))
    if wanted_numbers and actual_numbers and not wanted_numbers & actual_numbers:
        return None
    wanted_brands = {brand(name) for name in row["names"]} - {None}
    actual_brands = {brand(tags.get(key, "")) for key in ("name", "brand", "operator")} - {None}
    if wanted_brands & actual_brands and wanted_numbers & actual_numbers:
        return "same_brand_and_explicit_store_number"
    for wanted in row["names"]:
        for actual in actual_names:
            common = station_name_keys(wanted, row["City"]) & station_name_keys(actual, row["City"])
            chain = brand(wanted)
            # Missing native store number cannot confirm a numbered generic chain.
            if (not chain or not wanted_numbers or wanted_numbers & actual_numbers) and any(
                len(key) >= 7 and (chain is None or len(key) >= len(chain) + 6) for key in common
            ):
                return "exact_distinctive_station_name"
    return None


def nearby_points(anchor, points, tree, radius_m=3_000):
    latitude, longitude = anchor["latitude"], anchor["longitude"]
    longitude_radius = radius_m / (111_000 * max(0.05, math.cos(math.radians(latitude))))
    latitude_radius = radius_m / 110_000
    for index in tree.query(
        box(
            longitude - longitude_radius,
            latitude - latitude_radius,
            longitude + longitude_radius,
            latitude + latitude_radius,
        )
    ):
        point = points[int(index)]
        distance = abs(GEOD.inv(longitude, latitude, point["longitude"], point["latitude"])[2])
        if distance <= radius_m:
            yield point, round(distance, 1)


def build_proposals(rows, anchors, by_state):
    indexed_anchors = defaultdict(list)
    for anchor in anchors:
        indexed_anchors[(anchor["state"], anchor["route"], anchor["exit_ref"])].append(anchor)
    trees = {
        state: STRtree([Point(point["longitude"], point["latitude"]) for point in points])
        for state, points in by_state.items()
    }
    proposals, missed = [], []
    for identifier, row in rows.items():
        request = csv_exit_request(row["Address"])
        if not request:
            continue
        matches = [
            anchor
            for route in request["routes"]
            for anchor in indexed_anchors.get((row["State"], route, request["exit_ref"]), [])
        ]
        if not matches:
            missed.append(
                {
                    "opis_id": identifier,
                    "request": request,
                    "reason": "No exact route/exit/state anchor in the snapshot",
                }
            )
            continue
        candidates = {}
        points = by_state.get(row["State"], [])
        for anchor in matches:
            for point, distance in nearby_points(anchor, points, trees[row["State"]]):
                tags = point["tags"]
                if tags.get("amenity") != "fuel":
                    continue
                rule = strict_identity(row, point)
                actual_city = tags.get("addr:city", "")
                if actual_city and normalized(actual_city) != normalized(row["City"]):
                    rule = None
                key = (point["type"], point["id"])
                item = candidates.setdefault(
                    key, {"point": point, "identity_rule": rule, "exit_evidence": []}
                )
                item["exit_evidence"].append(
                    {
                        "junction_id": anchor["junction_id"],
                        "motorway_id": anchor["motorway_id"],
                        "route": anchor["route"],
                        "exit_ref": anchor["exit_ref"],
                        "distance_m": distance,
                    }
                )
        proposals.append(
            {
                "opis_id": identifier,
                "original_names": row["names"],
                "original_address": row["Address"],
                "original_city": row["City"],
                "original_state": row["State"],
                "request": request,
                "anchors": matches,
                "fuel_candidates": list(candidates.values()),
                "coordinate_verified": False,
                "reason": "Exact exit anchors narrow search only; review station identity and actual dispensing equipment before accepting any coordinate.",
            }
        )
    return proposals, missed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--download",
        action="store_true",
        help="Make the single bounded public acquisition if uncached",
    )
    parser.add_argument(
        "--published",
        action="store_true",
        help="Use the checksummed public OpenInterstate release instead of Overpass",
    )
    args = parser.parse_args()
    snapshot_path = SOURCES / "regional-exit-snapshot.json"
    if args.published:
        source_url = published_release(SOURCES)
        snapshot = {"elements": []}
        anchors = published_anchors(
            SOURCES / "regional-exit-published-exits.csv", state_boundaries(), source_url
        )
    else:
        source_url = ENDPOINT
        snapshot = (
            download_snapshot(SOURCES)
            if args.download
            else validate_snapshot(json.loads(snapshot_path.read_text(encoding="utf-8")))
        )
        anchors = extract_anchors(snapshot, state_boundaries())
    unresolved = {
        item["opis_id"]
        for item in json.loads((SOURCES / "unresolved.json").read_text(encoding="utf-8"))
    }
    rows = {identifier: row for identifier, row in stations().items() if identifier in unresolved}
    all_anchor_count = len(anchors)
    needed = {
        (row["State"], route, request["exit_ref"])
        for row in rows.values()
        if (request := csv_exit_request(row["Address"]))
        for route in request["routes"]
    }
    anchors = [
        anchor
        for anchor in anchors
        if (anchor["state"], anchor["route"], anchor["exit_ref"]) in needed
    ]
    write_json(SOURCES / "regional-exit-anchors.json", anchors)
    ambiguous_input = [
        {
            "opis_id": identifier,
            "original_address": row["Address"],
            "original_city": row["City"],
            "original_state": row["State"],
            "reason": "An exact single interstate exit cannot be parsed without guessing historical numbers, ranges or route association.",
        }
        for identifier, row in rows.items()
        if re.search(r"\bEXITS?\b", row["Address"], re.IGNORECASE)
        and csv_exit_request(row["Address"]) is None
    ]
    write_json(SOURCES / "regional-exit-ambiguous-input.json", ambiguous_input)
    proposals, missed = build_proposals(rows, anchors, load_pois())
    write_json(SOURCES / "regional-exit-proposals.json", proposals)
    write_json(SOURCES / "regional-exit-unmatched.json", missed)
    report = {
        "snapshot_elements": len(snapshot["elements"]),
        "anchors": len(anchors),
        "source_anchors": all_anchor_count,
        "unresolved_input": len(rows),
        "unresolved_with_exact_exit_syntax": sum(
            csv_exit_request(row["Address"]) is not None for row in rows.values()
        ),
        "anchored_stations": len(proposals),
        "stations_with_strict_identity_lead": sum(
            any(point["identity_rule"] for point in item["fuel_candidates"]) for item in proposals
        ),
        "unmatched_exact_exits": len(missed),
        "ambiguous_input_addresses": len(ambiguous_input),
        "accepted_coordinates": 0,
        "source_url": source_url,
        "osm_snapshot_timestamp": snapshot.get("osm3s", {}).get("timestamp_osm_base"),
        "documentation": DOCUMENTATION,
    }
    write_json(SOURCES / "regional-exit-report.json", report)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
