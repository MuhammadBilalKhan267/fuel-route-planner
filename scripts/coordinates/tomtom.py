"""Bounded, resumable offline POI research; licensed results are kept in memory.

The journal contains caller-supplied queries and request outcomes, never provider
place records. Search matches are proposals, not verified coordinate imports.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from pathlib import Path

import httpx
from dotenv import load_dotenv
from shapely.geometry import Point, shape

from scripts.coordinates.loves_ta import US_STATES, write_json
from scripts.coordinates.matching import brand, normalized, station_name, store_number
from scripts.coordinates.regional import normalized_address

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "data" / "coordinate-sources"
ENDPOINT = "https://api.tomtom.com/maps/orbis/places/discover"
DOCS_URL = "https://docs.tomtom.com/places-search-api/documentation/places-search/discover"
ATTRIBUTES = (
    "results.id,results.type,results.title,results.position.type,results.position.coordinates,"
    "results.address.countryCodeIso2,results.address.countrySubdivisionCodeIso,"
    "results.address.municipality,results.address.houseNumber,results.address.street,"
    "results.poiTypes.id,results.contacts.phones"
)


def valid_point(coordinates):
    return (
        isinstance(coordinates, (list, tuple))
        and len(coordinates) == 2
        and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            for value in coordinates
        )
        and -180 <= coordinates[0] <= 180
        and -90 <= coordinates[1] <= 90
    )


def validate_row(row):
    if (
        not isinstance(row, dict)
        or type(row.get("opis_id")) is not int
        or row["opis_id"] <= 0
        or row.get("state") not in US_STATES
        or not isinstance(row.get("city"), str)
        or not row["city"].strip()
        or not isinstance(row.get("address"), str)
        or not isinstance(row.get("names"), list)
        or not row["names"]
        or not all(isinstance(name, str) and name.strip() for name in row["names"])
    ):
        raise ValueError("Station queries require a positive ID, name, city, US state and address.")


def search_name(value):
    """Keep the business name searchable; preserve branch numbers in identity checks."""
    number = store_number(value)
    if number is not None:
        value = re.sub(rf"(?<!\d)#?\s*0*{re.escape(number)}\b", "", value)
    value = re.sub(
        r"\b(?:travel\s+(?:centers?|stops?|plazas?)|truck\s*stops?|fuel\s+stations?|"
        r"diesel\s+only)\b",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", value).strip(" ,-#")


def search_body(row, anchor=None, radius_m=50_000, query_mode="name"):
    validate_row(row)
    if type(radius_m) is not int or not 1 <= radius_m <= 1_000_000:
        raise ValueError("Search radius must be between 1 and 1,000,000 meters.")
    if query_mode == "name":
        query = search_name(row["names"][0]) or row["names"][0].strip()
    elif query_mode == "address":
        query = row.get("known_address", row["address"]).strip()
        if not re.match(r"^\d[\dA-Z-]*\s+", query, re.IGNORECASE):
            raise ValueError("Address searches require an actual numeric street, not an exit.")
    else:
        raise ValueError("Query mode must be name or address.")
    body = {
        "query": query,
        "maxResults": 10,
        "filters": {"types": ["poi"], "countryCodesIso2": ["US"], "poiTypes": ["fuel_station"]},
    }
    if anchor is not None:
        coordinates = [anchor.get("longitude"), anchor.get("latitude")]
        if not valid_point(coordinates):
            raise ValueError("Search anchor must contain finite longitude/latitude coordinates.")
        body["origin"] = {"type": "point", "coordinates": coordinates}
        body["preferences"] = {"geometry": {"type": "point", "coordinates": coordinates}}
        body["filters"]["geometry"] = {
            "type": "circle",
            "center": coordinates,
            "radiusInMeters": radius_m,
        }
    else:
        body["query"] += f", {row['city'].strip()}, {row['state']}"
    return body


def build_queue(rows, anchors=None, radius_m=50_000, query_mode="name"):
    entries, seen = [], set()
    for row in rows:
        validate_row(row)
        identifier = row["opis_id"]
        if identifier in seen:
            raise ValueError("A search queue cannot repeat a station ID.")
        seen.add(identifier)
        anchor = (anchors or {}).get(normalized(row["city"]) + "|" + row["state"])
        body = search_body(row, anchor, radius_m, query_mode)
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        entries.append(
            {"opis_id": identifier, "request_id": f"{identifier}-{digest[:16]}", "body": body}
        )
    return entries


def _phone(value):
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def identity_rule(row, result):
    """Require an exact branch identifier; a nearby/name-only result is a lead."""
    title = result.get("title", "")
    if not isinstance(title, str):
        return None
    expected_numbers = {store_number(name) for name in row["names"]} - {None}
    if len(expected_numbers) > 1:
        return None
    actual_number = store_number(title)
    if expected_numbers and actual_number and actual_number not in expected_numbers:
        return None
    expected_brands = {brand(name) for name in row["names"]} - {None}
    actual_brand = brand(title)
    same_brand = bool(expected_brands and actual_brand in expected_brands)
    same_name = station_name(title) in {station_name(name) for name in row["names"]}
    if expected_brands and not same_brand:
        return None
    if not (same_brand or same_name):
        return None
    if expected_numbers and actual_number in expected_numbers and same_brand:
        return "exact_brand_store_number_city_state"
    address = result.get("address") or {}
    expected_street = row.get("known_address", row["address"])
    actual_street = " ".join(str(address.get(key, "")) for key in ("houseNumber", "street"))
    if re.match(r"^\s*\d[\dA-Z-]*\s+", expected_street, re.IGNORECASE) and normalized_address(
        expected_street
    ) == normalized_address(actual_street):
        return "exact_name_or_brand_numeric_street_city_state"
    expected_phones = {_phone(value) for value in row.get("known_phones", [])} - {None}
    actual_phones = {
        _phone(value)
        for contact in result.get("contacts", [])
        if isinstance(contact, dict)
        for value in contact.get("phones", [])
        if isinstance(value, str)
    } - {None}
    if expected_phones & actual_phones:
        return "exact_name_or_brand_phone_city_state"
    return None


def response_results(payload):
    # Empty Discover responses have been observed as {} as well as results:[].
    if payload == {}:
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise TypeError("Discover returned an invalid results collection.")
    return payload["results"]


def assess_response(row, payload, state_boundaries):
    """Return in-memory proposals only; the caller owns storage permissions."""
    validate_row(row)
    matches, rejected = {}, Counter()
    for result in response_results(payload):
        if not isinstance(result, dict):
            rejected["invalid_result"] += 1
            continue
        address, position = result.get("address") or {}, result.get("position") or {}
        if not isinstance(address, dict) or not isinstance(position, dict):
            rejected["invalid_result"] += 1
            continue
        coordinates = position.get("coordinates")
        types = result.get("poiTypes") or []
        contacts = result.get("contacts") or []
        if (
            not isinstance(types, list)
            or not all(isinstance(item, dict) for item in types)
            or not isinstance(contacts, list)
            or not all(
                isinstance(contact, dict)
                and isinstance(contact.get("phones", []), list)
                and all(isinstance(phone, str) for phone in contact.get("phones", []))
                for contact in contacts
            )
            or not all(
                isinstance(address.get(key, ""), str)
                for key in ("countryCodeIso2", "countrySubdivisionCodeIso", "municipality")
            )
        ):
            rejected["invalid_result"] += 1
            continue
        if (
            result.get("type") != "poi"
            or not isinstance(result.get("id"), str)
            or not result["id"].strip()
            or "fuel_station" not in [item.get("id") for item in types]
        ):
            rejected["not_identified_fuel_poi"] += 1
            continue
        if position.get("type") != "Point" or not valid_point(coordinates):
            rejected["invalid_point"] += 1
            continue
        if (
            address.get("countryCodeIso2") != "US"
            or address.get("countrySubdivisionCodeIso") != "US-" + row["state"]
            or normalized(address.get("municipality", "")) != normalized(row["city"])
            or row["state"] not in state_boundaries
            or not state_boundaries[row["state"]].covers(Point(coordinates))
        ):
            rejected["wrong_city_state_or_country"] += 1
            continue
        if row.get("identity_review") or row.get("identity_collisions"):
            rejected["previous_identity_conflict"] += 1
            continue
        rule = identity_rule(row, result)
        if not rule:
            rejected["branch_identity_not_established"] += 1
            continue
        # Repeated identical result records do not create ambiguity; differing
        # points or addresses under the same ID still do.
        key = (str(result["id"]), tuple(coordinates), json.dumps(address, sort_keys=True))
        matches[key] = {
            "opis_id": row["opis_id"],
            "original_names": row["names"],
            "original_address": row["address"],
            "original_city": row["city"],
            "original_state": row["state"],
            "source": "tomtom_discover_search_proposal",
            "source_url": DOCS_URL,
            "external_id": str(result["id"]),
            "latitude": coordinates[1],
            "longitude": coordinates[0],
            "matched_name": result.get("title", ""),
            "matched_address": address,
            "identity_rule": rule,
            "coordinate_verified": False,
            "evidence": [rule, "US fuel POI; matching city/state and state boundary"],
            "notes": "Provider search result; requires independent coordinate evidence before import.",
        }
    status = (
        "unique_branch_proposal" if len(matches) == 1 else "ambiguous" if matches else "unresolved"
    )
    return {"status": status, "proposals": list(matches.values()), "rejected": dict(rejected)}


def run_queue(
    queue,
    journal_path,
    api_key,
    *,
    client,
    handle_result,
    max_requests=2_000,
    interval_seconds=1.0,
    max_seconds=2_400,
    clock=time.monotonic,
    sleep=time.sleep,
):
    """Perform each queued request once; journal requests before sending them.

    The lifetime budget includes previous runs and interrupted attempts. Errors
    halt the queue without retry; a halted journal requires explicit inspection.
    Provider bodies and exception messages never enter the journal or output.
    """
    if (
        type(max_requests) is not int
        or not 1 <= max_requests <= 5_000
        or not math.isfinite(interval_seconds)
        or interval_seconds < 1
        or not math.isfinite(max_seconds)
        or max_seconds <= 0
    ):
        raise ValueError(
            "The request budget must be 1 to 5,000. "
            "The interval must be at least 1 second and the duration positive."
        )
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("TOMTOM_API_KEY is required.")
    journal_path = Path(journal_path)
    journal = (
        json.loads(journal_path.read_text(encoding="utf-8"))
        if journal_path.exists()
        else {"version": 1, "attempts": [], "halted": None}
    )
    if journal.get("version") != 1 or not isinstance(journal.get("attempts"), list):
        raise ValueError("Unsupported search journal.")
    if journal.get("halted"):
        return {
            "requests_this_run": 0,
            "requests_total": len(journal["attempts"]),
            "stop": journal["halted"],
        }
    completed = {item["request_id"] for item in journal["attempts"]}
    started, last_request, sent = clock(), None, 0
    stop = "complete"
    headers = {"TomTom-Api-Key": api_key, "TomTom-Api-Version": "3", "Attributes": ATTRIBUTES}
    for entry in queue:
        if entry["request_id"] in completed:
            continue
        if len(journal["attempts"]) >= max_requests:
            stop = "request_budget"
            break
        remaining = (
            0 if last_request is None else max(0, interval_seconds - (clock() - last_request))
        )
        if clock() - started + remaining >= max_seconds:
            stop = "time_budget"
            break
        if remaining:
            sleep(remaining)
        attempt = {**entry, "status": "started"}
        journal["attempts"].append(attempt)
        write_json(journal_path, journal)
        sent += 1
        last_request = clock()
        try:
            response = client.post(ENDPOINT, headers=headers, json=entry["body"])
            attempt["http_status"] = response.status_code
            if response.status_code != 200:
                stop = f"http_{response.status_code}"
                attempt["status"] = "error"
                journal["halted"] = stop
                break
            payload = response.json()
            results = response_results(payload)
            decision = handle_result(entry, payload)
            attempt.update(status="completed", result_count=len(results))
            # The handler returns only a policy decision. Its full proposal is
            # processed in memory by the caller, never copied to the journal.
            if decision in {"unique_branch_proposal", "ambiguous", "unresolved"}:
                attempt["decision"] = decision
        except httpx.RequestError:
            stop = "transport_error"
            attempt["status"] = "error"
            journal["halted"] = stop
            break
        except (ValueError, TypeError, KeyError):
            stop = "invalid_response_or_handler"
            attempt["status"] = "error"
            journal["halted"] = stop
            break
        finally:
            write_json(journal_path, journal)
        completed.add(entry["request_id"])
    return {"requests_this_run": sent, "requests_total": len(journal["attempts"]), "stop": stop}


def valid_independent_candidate(candidate, context, boundaries):
    if not isinstance(candidate, dict) or candidate != context.get("candidate"):
        return False
    row = context["row"]
    coordinates = [candidate.get("longitude"), candidate.get("latitude")]
    return (
        candidate.get("opis_id") == row["opis_id"]
        and candidate.get("original_state") == row["state"]
        and valid_point(coordinates)
        and row["state"] in boundaries
        and boundaries[row["state"]].covers(Point(coordinates))
    )


def recover_independent(
    contexts,
    anchors,
    boundaries,
    api_key,
    *,
    client,
    crosscheck,
    journal_path,
    candidates_path,
    pending_ids=None,
    address_followups=False,
    max_requests=2_000,
    max_seconds=2_400,
    clock=time.monotonic,
    sleep=time.sleep,
):
    """Search both passes within one deadline; persist only independent records."""
    candidates_path = Path(candidates_path)
    existing = (
        json.loads(candidates_path.read_text(encoding="utf-8")) if candidates_path.exists() else []
    )
    if not isinstance(existing, list):
        raise TypeError("Independent candidates must be a list.")
    accepted = {
        candidate["opis_id"]: candidate
        for candidate in existing
        if isinstance(candidate, dict)
        and candidate.get("opis_id") in contexts
        and valid_independent_candidate(candidate, contexts[candidate["opis_id"]], boundaries)
    }
    current_candidates = sorted(accepted.values(), key=lambda item: item["opis_id"])
    if candidates_path.exists() and existing != current_candidates:
        # A halted or fully journaled queue can make no new requests. Remove
        # stale evidence now so its output cannot outlive current validation.
        write_json(candidates_path, current_candidates)
    previous_count = len(accepted)
    rows = [
        context["row"]
        for identifier, context in contexts.items()
        if identifier not in accepted and (pending_ids is None or identifier in pending_ids)
    ]
    decisions = Counter()
    deadline = clock() + max_seconds

    def review(entry, payload):
        context = contexts[entry["opis_id"]]
        assessment = assess_response(context["row"], payload, boundaries)
        decisions[assessment["status"]] += 1
        independent = crosscheck(context, payload, boundaries)
        # Persist the independently assembled record, never a provider-derived
        # proposal or fields returned by the transient search service.
        if valid_independent_candidate(independent, context, boundaries):
            accepted[entry["opis_id"]] = copy.deepcopy(context["candidate"])
            write_json(candidates_path, sorted(accepted.values(), key=lambda item: item["opis_id"]))
        return assessment["status"]

    first = run_queue(
        build_queue(rows, anchors),
        journal_path,
        api_key,
        client=client,
        handle_result=review,
        max_requests=max_requests,
        max_seconds=max_seconds,
        clock=clock,
        sleep=sleep,
    )
    requests = first["requests_this_run"]
    result = first
    if address_followups and first["stop"] == "complete":
        followups = [
            row
            for row in rows
            if row["opis_id"] not in accepted
            and re.match(
                r"^\s*\d[\dA-Z-]*\s+", row.get("known_address", row["address"]), re.IGNORECASE
            )
        ]
        if followups and deadline - clock() > 1:
            # run_queue owns its own throttle; preserve spacing at the phase boundary.
            sleep(1)
            result = run_queue(
                build_queue(followups, anchors, query_mode="address"),
                journal_path,
                api_key,
                client=client,
                handle_result=review,
                max_requests=max_requests,
                max_seconds=deadline - clock(),
                clock=clock,
                sleep=sleep,
            )
            requests += result["requests_this_run"]
        elif followups:
            result = {**first, "stop": "time_budget"}
    return {
        **result,
        "requests_this_run": requests,
        "independent_candidates_total": len(accepted),
        "independent_candidates_added": len(accepted) - previous_count,
        "decisions_this_run": dict(decisions),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--recover-independent", action="store_true")
    parser.add_argument("--address-followups", action="store_true")
    parser.add_argument("--max-requests", type=int, default=2_000)
    parser.add_argument("--max-seconds", type=float, default=2_400)
    parser.add_argument("--journal", type=Path, default=SOURCES / "tomtom-search-journal.json")
    args = parser.parse_args()
    if args.recover_independent and not args.fetch:
        parser.error("--recover-independent requires --fetch.")
    if args.address_followups and not args.recover_independent:
        parser.error("--address-followups requires --recover-independent.")
    rows = json.loads((SOURCES / "unresolved.json").read_text(encoding="utf-8"))
    anchor_path = SOURCES / "tomtom-search-anchors.json"
    if not anchor_path.exists():
        anchor_path = SOURCES / "city-search-anchors.json"
    anchors = json.loads(anchor_path.read_text(encoding="utf-8"))
    anchors = {
        normalized(anchor.get("city", key.split("|")[0]))
        + "|"
        + anchor.get("state", key.split("|")[-1]): anchor
        for key, anchor in anchors.items()
    }
    queue = build_queue(rows, anchors)
    unanchored = sum("origin" not in entry["body"] for entry in queue)
    if not args.fetch:
        print({"queued_stations": len(queue), "unanchored_stations": unanchored, "requests": 0})
        return
    load_dotenv(ROOT / ".env", override=False)
    boundaries = {
        feature["properties"]["STUSAB"]: shape(feature["geometry"])
        for feature in json.loads((ROOT / "trips/data/us-states.geojson").read_text())["features"]
    }
    by_id = {row["opis_id"]: row for row in rows}
    decisions = Counter()

    def review(entry, payload):
        decision = assess_response(by_id[entry["opis_id"]], payload, boundaries)
        decisions[decision["status"]] += 1
        return decision["status"]

    with httpx.Client(timeout=30, follow_redirects=False) as client:
        if args.recover_independent:
            from scripts.coordinates.match_osm import stations
            from scripts.coordinates.tomtom_recovery import build_contexts, crosscheck_independent

            candidates_path = SOURCES / "tomtom-independent-candidates.json"
            previous = (
                json.loads(candidates_path.read_text(encoding="utf-8"))
                if candidates_path.exists()
                else []
            )
            if not isinstance(previous, list):
                raise TypeError("Independent candidates must be a list.")
            context_ids = set(by_id) | {
                candidate["opis_id"]
                for candidate in previous
                if isinstance(candidate, dict) and type(candidate.get("opis_id")) is int
            }
            original_rows = [
                {
                    "opis_id": identifier,
                    "names": original["names"],
                    "address": original["Address"],
                    "city": original["City"],
                    "state": original["State"],
                }
                for identifier, original in stations().items()
                if identifier in context_ids and original["State"] in US_STATES
            ]
            contexts = build_contexts(SOURCES, context_ids, original_rows=original_rows)
            result = recover_independent(
                contexts,
                anchors,
                boundaries,
                os.environ.get("TOMTOM_API_KEY", ""),
                client=client,
                crosscheck=crosscheck_independent,
                journal_path=args.journal,
                candidates_path=candidates_path,
                pending_ids=set(by_id),
                address_followups=args.address_followups,
                max_requests=args.max_requests,
                max_seconds=args.max_seconds,
            )
        else:
            result = run_queue(
                queue,
                args.journal,
                os.environ.get("TOMTOM_API_KEY", ""),
                client=client,
                handle_result=review,
                max_requests=args.max_requests,
                max_seconds=args.max_seconds,
            )
            result["decisions_this_run"] = dict(decisions)
    print({**result, "unanchored_stations": unanchored})


if __name__ == "__main__":
    main()
