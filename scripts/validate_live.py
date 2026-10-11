"""Run bounded, opt-in provider integration checks against the imported dataset."""

import argparse
import json
import os
import time
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]


def validate(result, stations):
    fuel, route = result["fuel"], result["route"]
    decimal = Decimal
    tolerance = decimal("0.000002")
    remaining = decimal(fuel["initial_fuel_gallons"])
    previous = decimal(0)
    purchases = decimal(0)
    cost = decimal("0.00")
    for stop in fuel["stops"]:
        station = stations[stop["station_id"]]
        assert station.coordinate_verified and station.operational_status != "closed"
        position, quantity = decimal(stop["route_mile"]), decimal(stop["gallons_purchased"])
        remaining -= (position - previous) / 10
        assert (
            remaining >= -tolerance
            and abs(remaining - decimal(stop["arrival_fuel_gallons"])) <= tolerance
        )
        remaining += quantity
        assert quantity > 0 and remaining <= 50 + tolerance
        assert abs(remaining - decimal(stop["departure_fuel_gallons"])) <= tolerance
        expected = (quantity * decimal(stop["price_usd_per_gallon"])).quantize(
            decimal("0.01"), ROUND_HALF_UP
        )
        assert abs(expected - decimal(stop["cost_usd"])) <= decimal("0.01")
        purchases += quantity
        cost += decimal(stop["cost_usd"])
        previous = position
    remaining -= (decimal(route["distance_miles"]) - previous) / 10
    assert remaining >= -tolerance
    assert abs(remaining - decimal(fuel["ending_fuel_gallons"])) <= tolerance
    assert abs(purchases - decimal(fuel["gallons_purchased"])) <= tolerance
    assert (
        abs(decimal(route["distance_miles"]) / 10 - decimal(fuel["gallons_consumed"])) <= tolerance
    )
    assert cost == decimal(fuel["purchase_cost_usd"])
    assert cost + decimal(fuel["initial_fuel_cost_usd"]) == decimal(fuel["total_cost_usd"])
    assert route["geometry"]["type"] == "LineString" and len(route["geometry"]["coordinates"]) > 1
    assert result["metadata"]["routing_calls"] <= 3 and result["metadata"]["geocoding_calls"] <= 2
    if result["planning"]["verify_stops_requested"]:
        assert result["planning"]["road_distances_verified"]


def main(network, cached_only=False):
    if not network and not cached_only:
        raise SystemExit("Choose --network for live checks or --cached-only for saved responses.")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fuelroute.settings")
    import django

    django.setup()
    from django.test import Client

    from trips.models import FuelStation
    from trips.provider import HeiGitClient
    from trips.stations import dataset_status

    ledger = {
        "started_at": datetime.now(UTC).isoformat(),
        "mode": "cached_only" if cached_only else "live",
        "dataset": dataset_status(),
        "http_calls": [],
        "blocked_request_paths": [],
        "cases": [],
        "complete": False,
    }
    stations = FuelStation.objects.in_bulk(field_name="opis_id")
    last_request = 0.0
    halted = False

    def before_request(request):
        nonlocal last_request
        if cached_only:
            return
        if halted or len(ledger["http_calls"]) >= 12:
            raise RuntimeError("Live validation stopped at its HTTP budget or provider error.")
        time.sleep(max(0, 4 - (time.monotonic() - last_request)))
        last_request = time.monotonic()
        ledger["http_calls"].append({"path": request.url.path, "method": request.method})

    def after_response(response):
        nonlocal halted
        ledger["http_calls"][-1].update(
            status=response.status_code,
            quota={
                key: value
                for key, value in response.headers.items()
                if key.startswith("x-ratelimit-")
            },
        )
        if response.status_code in {401, 403, 429}:
            halted = True

    def block_network(request):
        ledger["blocked_request_paths"].append(request.url.path)
        raise httpx.ConnectError(
            "Cached-only validation requires a saved response.", request=request
        )

    long_trip = {
        "start": {"latitude": 41.8781, "longitude": -87.6298},
        "finish": {"latitude": 29.7604, "longitude": -95.3698},
    }
    cases = [
        ("long_verified", long_trip),
        ("long_cached", long_trip),
        ("long_projection", {**long_trip, "verify_stops": False}),
        ("long_initial_value", {**long_trip, "initial_fuel_price_usd_per_gallon": "3.50"}),
        (
            "coast_to_coast_verified",
            {
                "start": {"latitude": 40.7128, "longitude": -74.0060},
                "finish": {"latitude": 34.0522, "longitude": -118.2437},
            },
        ),
        (
            "text_street_addresses",
            {
                "start": "200 N Spring St, Los Angeles, CA",
                "finish": "1 Dr Carlton B Goodlett Pl, San Francisco, CA",
            },
        ),
    ]
    if cached_only:
        cases = [
            (
                f"cached_base_route_{name.removesuffix('_verified')}",
                {**body, "verify_stops": False},
            )
            for name, body in cases
            if name != "long_projection"
        ]
    destination = ROOT / (
        "data/cached-verified-stations" if cached_only else "data/live-verified-stations"
    )
    destination.mkdir(exist_ok=True)
    try:
        with (
            httpx.Client(
                timeout=35,
                follow_redirects=False,
                transport=httpx.MockTransport(block_network) if cached_only else None,
                event_hooks={"request": [before_request], "response": [after_response]},
            ) as http,
            patch(
                "trips.views.HeiGitClient", side_effect=lambda counts: HeiGitClient(counts, http)
            ),
        ):
            client = Client(HTTP_HOST="localhost")
            for name, body in cases:
                started = time.perf_counter()
                response = client.post(
                    "/api/routes/", data=json.dumps(body), content_type="application/json"
                )
                result = response.json()
                (destination / f"{name}.json").write_text(
                    json.dumps(result) + "\n", encoding="utf-8"
                )
                summary = {
                    "name": name,
                    "status": response.status_code,
                    "seconds": round(time.perf_counter() - started, 3),
                    "metadata": result.get("metadata"),
                }
                if response.status_code == 200:
                    validate(result, stations)
                    summary.update(
                        distance_miles=result["route"]["distance_miles"],
                        stops=len(result["fuel"]["stops"]),
                        total_cost=result["fuel"]["total_cost_usd"],
                    )
                    if name.endswith("long_cached"):
                        assert (
                            result["metadata"]["routing_calls"]
                            == result["metadata"]["geocoding_calls"]
                            == 0
                        )
                    if name in {"long_verified", "cached_base_route_long"}:
                        assert len(result["fuel"]["stops"]) >= 2
                else:
                    summary["error"] = result.get("error")
                ledger["cases"].append(summary)
                print(summary, flush=True)
                if response.status_code != 200 or halted:
                    raise RuntimeError(
                        "Validation stopped after an unsuccessful case; inspect its saved response."
                    )
        ledger["complete"] = True
    finally:
        ledger["finished_at"] = datetime.now(UTC).isoformat()
        ledger["note"] = (
            "Network transport is disabled. These checks plan purchases against cached base routes with stop verification disabled; they do not validate new road itineraries."
            if cached_only
            else "Cold elapsed times include deliberate four-second spacing of external calls. Warm times have no throttle delay. No station preparation runs in a trip request."
        )
        (destination / "validation.json").write_text(
            json.dumps(ledger, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--network", action="store_true")
    modes.add_argument("--cached-only", action="store_true")
    args = parser.parse_args()
    main(args.network, args.cached_only)
