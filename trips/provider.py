"""HeiGIT routing/geocoding boundary with persistent cache and strict call budget."""

import hashlib
import json
import math
import pickle
import zlib
from dataclasses import dataclass
from decimal import Decimal

import httpx
from django.conf import settings
from django.core.cache import cache

from .errors import PlanningError
from .fuel import miles
from .geometry import GEOD, METERS_PER_MILE, in_usa

MAX_ROUTING_CALLS = 3
MAX_WAYPOINTS = 50
MAX_SNAPPING_METERS = 350
# Boundary centroids from Who's on First can fall far from drivable roads.
GEOCODE_SOURCES = "openstreetmap,openaddresses,geonames"


@dataclass
class CallCounts:
    routing: int = 0
    geocoding: int = 0
    routing_cache_hits: int = 0
    geocoding_cache_hits: int = 0

    def as_dict(self):
        return {
            "routing_calls": self.routing,
            "geocoding_calls": self.geocoding,
            "routing_cache_hits": self.routing_cache_hits,
            "geocoding_cache_hits": self.geocoding_cache_hits,
        }


@dataclass(frozen=True)
class Endpoint:
    longitude: float
    latitude: float
    label: str

    @property
    def coordinates(self):
        return [self.longitude, self.latitude]


@dataclass(frozen=True)
class Route:
    coordinates: tuple[tuple[float, float], ...]
    leg_meters: tuple[Decimal, ...]
    waypoint_indices: tuple[int, ...]
    duration_seconds: float

    @property
    def leg_miles(self):
        return tuple(miles(length / Decimal(str(METERS_PER_MILE))) for length in self.leg_meters)

    @property
    def distance_miles(self):
        return sum(self.leg_miles, Decimal(0))

    def vertex_miles(self):
        result = [0.0] * len(self.coordinates)
        total = 0.0
        for length, start, finish in zip(
            self.leg_miles, self.waypoint_indices, self.waypoint_indices[1:]
        ):
            lengths = [
                abs(GEOD.inv(*a, *b)[2])
                for a, b in zip(
                    self.coordinates[start:finish], self.coordinates[start + 1 : finish + 1]
                )
            ]
            geometric_length = sum(lengths)
            if geometric_length == 0 and length:
                raise PlanningError(
                    "invalid_provider_response",
                    "Route geometry cannot represent its reported distance.",
                    502,
                )
            result[start] = total
            traveled = 0.0
            for index, segment_length in enumerate(lengths, start + 1):
                traveled += segment_length
                result[index] = total + (
                    traveled / geometric_length * float(length) if geometric_length else 0
                )
            total += float(length)
            result[finish] = total
        return result

    @property
    def geojson(self):
        return {"type": "LineString", "coordinates": [list(point) for point in self.coordinates]}


def validate_waypoint_snapping(route, coordinates):
    for requested, index in zip(coordinates, route.waypoint_indices, strict=True):
        snapped = route.coordinates[index]
        # Allow one metre for the provider's rounded geometry coordinates.
        if abs(GEOD.inv(*requested, *snapped)[2]) > MAX_SNAPPING_METERS + 1:
            raise PlanningError(
                "invalid_provider_response",
                "The returned route does not reach a requested waypoint within 350 metres.",
                502,
            )


def cache_key(kind, payload):
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"fuelroute:provider-v1:{kind}:{digest}"


def _cached_response(key):
    try:
        return cache.get(key)
    except (EOFError, pickle.UnpicklingError, zlib.error):
        cache.delete(key)
        return None


def coordinate_pair(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("Expected [longitude, latitude].")
    if any(
        isinstance(number, bool) or not isinstance(number, (int, float, Decimal))
        for number in value
    ):
        raise ValueError("Coordinates must be numbers.")
    try:
        lon, lat = map(float, value)
    except OverflowError as exc:
        raise ValueError("Coordinates are outside their valid range.") from exc
    if (
        not math.isfinite(lon)
        or not math.isfinite(lat)
        or not -180 <= lon <= 180
        or not -90 <= lat <= 90
    ):
        raise ValueError("Coordinates are outside their valid range.")
    return lon, lat


def _confidence(properties):
    value = properties.get("confidence", 0)
    confidence = float(value)
    if isinstance(value, bool) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("Invalid geocoding confidence.")
    return confidence


def _exact_city_match(properties, text):
    if properties.get("layer") not in ("locality", "localadmin"):
        return False
    if properties.get("match_type") != "exact":
        return False
    name = properties.get("name")
    if not isinstance(name, str) or not name.strip():
        return False

    def normalized(value):
        return " ".join(value.replace(",", " ").replace(".", "").casefold().split())

    names = {normalized(name)}
    for region in (properties.get("region_a"), properties.get("region")):
        if isinstance(region, str) and region.strip():
            names.add(normalized(f"{name} {region}"))
    names |= {f"{value} {country}" for value in names for country in ("usa", "us", "united states")}
    return normalized(text) in names


class HeiGitClient:
    def __init__(self, counts, http=None):
        self.counts = counts
        self.http = http or httpx.Client(
            timeout=settings.PROVIDER_TIMEOUT_SECONDS, follow_redirects=False
        )
        self.owns_http = http is None

    def close(self):
        if self.owns_http:
            self.http.close()

    def _request(self, method, path, **kwargs):
        if not settings.ORS_API_KEY:
            raise PlanningError(
                "provider_not_configured", "Set ORS_API_KEY to use the free HeiGIT provider.", 503
            )
        if path.startswith("/pelias/"):
            self.counts.geocoding += 1
        else:
            self.counts.routing += 1
        try:
            response = self.http.request(
                method,
                settings.ORS_API_BASE_URL + path,
                headers={"Authorization": settings.ORS_API_KEY},
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise PlanningError("provider_timeout", "The map provider timed out.", 504) from exc
        except httpx.RequestError as exc:
            raise PlanningError(
                "provider_unavailable", "The map provider is unavailable.", 503
            ) from exc
        if response.status_code == 429:
            raise PlanningError(
                "provider_rate_limited", "The provider rate limit was reached.", 503
            )
        if response.status_code in (401, 403):
            raise PlanningError(
                "provider_access_denied", "The configured key lacks provider access or quota.", 503
            )
        if response.status_code >= 500:
            raise PlanningError("provider_unavailable", "The map provider is unavailable.", 503)
        try:
            payload = response.json()
        except (ValueError, RecursionError) as exc:
            raise PlanningError(
                "invalid_provider_response", "The provider returned invalid JSON.", 502
            ) from exc
        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            if code == 2010:
                raise PlanningError(
                    "no_route",
                    "An endpoint or fuel stop cannot be connected to a road. "
                    "Use a street address or road-accessible coordinates.",
                )
            if code == 2009:
                raise PlanningError(
                    "no_route", "No driving route could be found for these locations."
                )
            raise PlanningError(
                "provider_rejected_request", "The provider could not process these locations."
            )
        if not isinstance(payload, dict):
            raise PlanningError(
                "invalid_provider_response",
                "The provider returned an unexpected response shape.",
                502,
            )
        return payload

    def geocode(self, text):
        key = cache_key(
            "geocode",
            {
                "base": settings.ORS_API_BASE_URL,
                "text": " ".join(text.casefold().split()),
                "sources": GEOCODE_SOURCES,
            },
        )
        cached = _cached_response(key)
        if cached is not None:
            self.counts.geocoding_cache_hits += 1
            return cached
        if self.counts.geocoding >= 2:
            raise PlanningError(
                "geocoding_budget_exhausted", "Only two endpoint geocoding calls are allowed.", 502
            )
        payload = self._request(
            "GET",
            "/pelias/v1/search",
            params={
                "text": text,
                "boundary.country": "USA",
                "size": 3,
                "sources": GEOCODE_SOURCES,
            },
        )
        features = payload.get("features")
        if not isinstance(features, list):
            raise PlanningError(
                "invalid_provider_response", "The geocoder returned no feature list.", 502
            )
        if not features:
            raise PlanningError(
                "location_not_found", "No US location was found for the supplied text."
            )
        try:
            feature = features[0]
            if feature["geometry"]["type"] != "Point":
                raise ValueError("Geocoding geometry must be a Point.")
            lon, lat = coordinate_pair(feature["geometry"]["coordinates"])
            properties = feature["properties"]
            country = properties.get("country_a", "").upper()
            if country not in ("USA", "US") or not in_usa(lon, lat):
                raise PlanningError(
                    "location_outside_usa", "The resolved location is outside the USA."
                )
            confidence = _confidence(properties)
            if confidence < 0.8:
                raise PlanningError(
                    "ambiguous_location", "Include a state or a more complete address."
                )
            city_match = _exact_city_match(properties, text)
            if properties.get("layer") in ("locality", "localadmin") and not city_match:
                raise PlanningError(
                    "ambiguous_location",
                    "The geocoder matched only a city. Use its full name and state, "
                    "or a complete street address.",
                )
            for alternative in features[1:]:
                other_properties = alternative.get("properties", {})
                other_confidence = _confidence(other_properties)
                other_lon, other_lat = coordinate_pair(alternative["geometry"]["coordinates"])
                # GeoNames can assign confidence 1 to a substring (Dallas/Lake Dallas).
                if city_match and not _exact_city_match(other_properties, text):
                    continue
                if (
                    confidence - other_confidence < 0.05
                    and abs(GEOD.inv(lon, lat, other_lon, other_lat)[2]) > 40_000
                ):
                    raise PlanningError(
                        "ambiguous_location",
                        "Several distant locations match; include a state or full address.",
                    )
            label = properties["label"]
            if not isinstance(label, str) or not label.strip():
                raise ValueError("Invalid location label.")
            result = Endpoint(lon, lat, label)
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError) as exc:
            raise PlanningError(
                "invalid_provider_response", "The geocoder returned an invalid location.", 502
            ) from exc
        cache.set(key, result, settings.GEOCODE_CACHE_SECONDS)
        return result

    def directions(self, coordinates):
        if not 2 <= len(coordinates) <= MAX_WAYPOINTS:
            raise PlanningError(
                "too_many_stops", "The free provider supports at most 48 fuel-stop waypoints."
            )
        coordinates = [list(coordinate_pair(point)) for point in coordinates]
        key = cache_key(
            "route",
            {
                "base": settings.ORS_API_BASE_URL,
                "coordinates": coordinates,
                "profile": "driving-car",
                "snapping_radius_meters": MAX_SNAPPING_METERS,
            },
        )
        cached = _cached_response(key)
        if cached is not None:
            validate_waypoint_snapping(cached, coordinates)
            self.counts.routing_cache_hits += 1
            return cached
        if self.counts.routing >= MAX_ROUTING_CALLS:
            raise PlanningError(
                "routing_budget_exhausted", "The three-call routing budget was exhausted.", 502
            )
        payload = self._request(
            "POST",
            "/openrouteservice/v2/directions/driving-car/geojson",
            json={
                "coordinates": coordinates,
                "radiuses": [MAX_SNAPPING_METERS] * len(coordinates),
                # ORS includes road-leg distances only with instructions enabled.
                "instructions": True,
                "geometry_simplify": False,
            },
        )
        try:
            features = payload["features"]
            if not isinstance(features, list):
                raise TypeError("Route features must be a list.")
            if not features:
                raise PlanningError("no_route", "The provider found no driving route.")
            feature = features[0]
            if feature["geometry"]["type"] != "LineString":
                raise ValueError("Route must be a LineString.")
            points = tuple(coordinate_pair(point) for point in feature["geometry"]["coordinates"])
            properties = feature["properties"]
            legs = tuple(Decimal(str(segment["distance"])) for segment in properties["segments"])
            waypoint_indices = tuple(properties["way_points"])
            duration = float(properties["summary"]["duration"])
            total_distance = Decimal(str(properties["summary"]["distance"]))
            if (
                len(points) < 2
                or len(legs) != len(coordinates) - 1
                or len(waypoint_indices) != len(coordinates)
            ):
                raise ValueError("Route leg/waypoint count mismatch.")
            if (
                any(not length.is_finite() or length < 0 for length in legs)
                or not math.isfinite(duration)
                or duration < 0
            ):
                raise ValueError("Invalid route metrics.")
            if (
                not total_distance.is_finite()
                or total_distance < 0
                or abs(total_distance - sum(legs)) > Decimal("0.1") * (len(legs) + 1)
            ):
                raise ValueError("Route summary does not agree with its leg distances.")
            if any(
                isinstance(index, bool) or not isinstance(index, int) for index in waypoint_indices
            ):
                raise ValueError("Invalid waypoint indices.")
            if (
                waypoint_indices[0] != 0
                or waypoint_indices[-1] != len(points) - 1
                or list(waypoint_indices) != sorted(waypoint_indices)
            ):
                raise ValueError("Invalid waypoint order.")
            route = Route(points, legs, waypoint_indices, duration)
            route.vertex_miles()
            validate_waypoint_snapping(route, coordinates)
        except (KeyError, TypeError, ValueError, IndexError, ArithmeticError) as exc:
            raise PlanningError(
                "invalid_provider_response", "The routing provider returned an invalid route.", 502
            ) from exc
        cache.set(key, route, settings.ROUTE_CACHE_SECONDS)
        return route
