from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase, override_settings

from trips.fuel import LocatedStation, Station
from trips.importing import refresh_revision
from trips.models import FuelStation
from trips.provider import Route
from trips.stations import station_index

TEST_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fuelroute-tests",
    }
}


@override_settings(
    CACHES=TEST_CACHE,
    ORS_API_KEY="test-key",
    DEFAULT_INITIAL_FUEL_GALLONS="50",
    DEFAULT_INITIAL_FUEL_PRICE_USD="0",
    DEFAULT_VERIFY_STOPS=True,
    DEFAULT_STATION_CORRIDOR_MILES="2",
)
class CachedTestCase(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        station_index.cache_clear()

    def tearDown(self):
        cache.clear()
        station_index.cache_clear()
        super().tearDown()


def located(identifier, position, price, longitude=-97, latitude=35):
    return LocatedStation(
        Station(identifier, f"Station {identifier}", Decimal(str(price)), latitude, longitude),
        Decimal(str(position)),
    )


def route(points, leg_miles, indices=None):
    if indices is None:
        indices = range(len(points))
    return Route(
        tuple(map(tuple, points)),
        tuple(Decimal(str(length)) * Decimal("1609.344") for length in leg_miles),
        tuple(indices),
        123,
    )


def route_payload(points, leg_miles, indices=None):
    value = route(points, leg_miles, indices)
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": value.geojson,
                "properties": {
                    "segments": [{"distance": float(length)} for length in value.leg_meters],
                    "way_points": list(value.waypoint_indices),
                    "summary": {
                        "distance": float(sum(value.leg_meters)),
                        "duration": value.duration_seconds,
                    },
                },
            }
        ],
    }


def geocode_payload(longitude=-97, latitude=35, confidence=1, label="Oklahoma, USA", country="USA"):
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [longitude, latitude]},
                "properties": {"confidence": confidence, "label": label, "country_a": country},
            }
        ],
    }


def create_station(identifier=1, longitude=-97, latitude=35, price="3", **kwargs):
    kwargs.setdefault("coordinate_verified", latitude is not None and longitude is not None)
    value = FuelStation.objects.create(
        opis_id=identifier,
        name=f"Station {identifier}",
        address="1 Main St",
        city="Oklahoma City",
        state="OK",
        price=Decimal(price),
        longitude=longitude,
        latitude=latitude,
        coordinate_source="test_address_match",
        **kwargs,
    )
    refresh_revision()
    return value
