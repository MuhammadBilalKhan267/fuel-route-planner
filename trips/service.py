"""One request's orchestration; routing correction is bounded at three calls."""

from decimal import Decimal

from .errors import PlanningError
from .fuel import CAPACITY_GALLONS, MAX_RANGE_MILES, MPG, LocatedStation, optimize
from .stations import dataset_status, station_index


def _verified_plan(route, purchases, options):
    position = Decimal(0)
    stations = []
    for leg, purchase in zip(route.leg_miles, purchases):
        position += leg
        stations.append(
            LocatedStation(purchase.location.station, position, purchase.location.deviation_miles)
        )
    return optimize(stations, route.distance_miles, options.initial_fuel, options.initial_price)


def _waypoints(start, finish, purchases):
    return [
        start.coordinates,
        *[[p.location.station.longitude, p.location.station.latitude] for p in purchases],
        finish.coordinates,
    ]


def plan_trip(request, provider):
    start = provider.geocode(request.start) if isinstance(request.start, str) else request.start
    finish = provider.geocode(request.finish) if isinstance(request.finish, str) else request.finish
    if start.coordinates == finish.coordinates:
        raise PlanningError(
            "identical_locations", "Start and finish resolve to the same location.", 400
        )
    status = dataset_status()
    index = station_index(status["revision"])
    options = request.options
    route = provider.directions([start.coordinates, finish.coordinates])
    candidates = index.along_route(route, options.corridor_miles)
    try:
        plan = optimize(
            candidates, route.distance_miles, options.initial_fuel, options.initial_price
        )
    except PlanningError as exc:
        exc.details.update(status)
        raise

    verified = not plan.purchases
    itinerary_ids = []
    correction_used = False
    if options.verify_stops and plan.purchases:
        requested = plan.purchases
        itinerary_ids = [p.location.station.opis_id for p in requested]
        route = provider.directions(_waypoints(start, finish, requested))
        try:
            plan = _verified_plan(route, requested, options)
        except PlanningError as exc:
            if exc.code != "fuel_plan_unavailable":
                raise
            # Reconsider the local corridor around the first verified route;
            # candidate positions remain estimates until the final request.
            candidates = index.along_route(route, options.corridor_miles)
            try:
                revised = optimize(
                    candidates, route.distance_miles, options.initial_fuel, options.initial_price
                )
            except PlanningError as failed:
                raise PlanningError(
                    "verified_plan_unavailable",
                    "No feasible itinerary could be verified within the routing budget.",
                    details=status,
                ) from failed
            requested = revised.purchases
            route = provider.directions(_waypoints(start, finish, requested))
            itinerary_ids = [p.location.station.opis_id for p in requested]
            correction_used = True
            try:
                plan = _verified_plan(route, requested, options)
            except PlanningError as failed:
                raise PlanningError(
                    "verified_plan_unavailable",
                    "The corrected itinerary exceeds available fuel.",
                    details=status,
                ) from failed
        else:
            used_ids = [p.location.station.opis_id for p in plan.purchases]
            if used_ids != itinerary_ids:
                # Remove now-unneeded waypoint detours once; never silently
                # exceed the three-request cap to chase a changing optimum.
                requested = plan.purchases
                route = provider.directions(_waypoints(start, finish, requested))
                itinerary_ids = used_ids
                correction_used = True
                try:
                    plan = _verified_plan(route, requested, options)
                except PlanningError as failed:
                    raise PlanningError(
                        "verified_plan_unavailable",
                        "The final itinerary exceeds available fuel.",
                        details=status,
                    ) from failed
        verified = True

    return serialize(
        start,
        finish,
        route,
        plan,
        options,
        status,
        provider.counts,
        verified,
        itinerary_ids,
        correction_used,
    )


def number(value, places):
    return format(value, f".{places}f")


def serialize(
    start, finish, route, plan, options, status, counts, verified, itinerary_ids, correction_used
):
    stops = []
    for sequence, purchase in enumerate(plan.purchases, 1):
        item = purchase.location
        station = item.station
        stops.append(
            {
                "sequence": sequence,
                "station_id": station.opis_id,
                "station_alias_ids": list(station.opis_aliases or (station.opis_id,)),
                "price_observations": station.price_observations,
                "name": station.name,
                "address": station.address,
                "city": station.city,
                "state": station.state,
                "location": {"latitude": station.latitude, "longitude": station.longitude},
                "coordinate_source": station.coordinate_source,
                "price_usd_per_gallon": number(station.price, 8),
                "route_mile": number(item.mile, 6),
                "straight_line_deviation_miles": round(item.deviation_miles, 4),
                "gallons_purchased": number(purchase.gallons, 7),
                "arrival_fuel_gallons": number(purchase.arrival_fuel, 7),
                "departure_fuel_gallons": number(purchase.departure_fuel, 7),
                "cost_usd": number(purchase.cost, 2),
            }
        )
    return {
        "start": {"label": start.label, "coordinates": start.coordinates},
        "finish": {"label": finish.label, "coordinates": finish.coordinates},
        "route": {
            "geometry": route.geojson,
            "distance_miles": number(plan.distance, 6),
            "duration_seconds": route.duration_seconds,
            "via_station_ids": itinerary_ids,
        },
        "fuel": {
            "maximum_range_miles": int(MAX_RANGE_MILES),
            "mpg": int(MPG),
            "tank_capacity_gallons": int(CAPACITY_GALLONS),
            "initial_fuel_gallons": number(plan.initial_fuel, 7),
            "initial_fuel_price_usd_per_gallon": number(plan.initial_price, 8),
            "initial_fuel_cost_usd": number(plan.initial_cost, 2),
            "gallons_consumed": number(plan.distance / MPG, 7),
            "gallons_purchased": number(plan.gallons_purchased, 7),
            "ending_fuel_gallons": number(plan.ending_fuel, 7),
            "purchase_cost_usd": number(plan.purchase_cost, 2),
            "total_cost_usd": number(plan.total_cost, 2),
            "stops": stops,
        },
        "planning": {
            "verify_stops_requested": options.verify_stops,
            "road_distances_verified": verified,
            "distance_model": "provider_road_legs" if verified else "base_route_station_projection",
            "optimization": "greedy_fixed_itinerary",
            "globally_optimal_road_route": False,
            "station_corridor_miles": number(options.corridor_miles, 6),
            "correction_used": correction_used,
            "price_policy": "arithmetic_mean_of_csv_rows_per_verified_physical_station",
            "initial_cost_policy": "full_initial_quantity_times_configured_price",
        },
        "metadata": {**counts.as_dict(), "station_dataset": status},
        "attribution": "Routing: openrouteservice / HeiGIT. Map data: OpenStreetMap contributors.",
    }
