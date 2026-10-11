from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings

from .errors import PlanningError
from .fuel import CAPACITY_GALLONS
from .geometry import in_usa
from .provider import Endpoint, coordinate_pair


@dataclass(frozen=True)
class TripOptions:
    initial_fuel: Decimal
    initial_price: Decimal
    verify_stops: bool
    corridor_miles: Decimal


@dataclass(frozen=True)
class TripRequest:
    start: Endpoint | str
    finish: Endpoint | str
    options: TripOptions


def invalid(message):
    return PlanningError("invalid_request", message, 400)


def decimal_option(value, field, maximum, places, allow_zero=True):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float, str, Decimal))
        or len(str(value)) > 32
    ):
        raise invalid(f"{field} must be a finite number.")
    try:
        number = Decimal(str(value))
        if (
            not number.is_finite()
            or number < 0
            or number > maximum
            or (not allow_zero and number == 0)
        ):
            raise invalid(
                f"{field} must be {'between 0 and' if allow_zero else 'greater than 0 and at most'} {maximum}."
            )
        if number == 0:
            return Decimal(0)
        parts = number.as_tuple()
        exponent = parts.exponent
        for digit in reversed(parts.digits):
            if digit:
                break
            exponent += 1
        if exponent < -places:
            raise invalid(f"{field} supports at most {places} decimal places.")
        return number
    except InvalidOperation as exc:
        raise invalid(f"{field} must be a finite number.") from exc


def parse_location(value, name):
    if isinstance(value, str):
        text = value.strip()
        if not 1 <= len(text) <= 300:
            raise invalid(f"{name} text must contain 1 to 300 characters.")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise invalid(f"{name} text must contain valid Unicode characters.") from exc
        return text
    if not isinstance(value, dict):
        raise invalid(f"{name} must be text or a location object.")
    if set(value) == {"address"}:
        if not isinstance(value["address"], str):
            raise invalid(f"{name}.address must be text.")
        return parse_location(value["address"], name)
    if set(value) != {"latitude", "longitude"}:
        raise invalid(
            f"{name} requires either address or latitude and longitude, without extra fields."
        )
    try:
        lon, lat = coordinate_pair([value["longitude"], value["latitude"]])
    except (ValueError, TypeError, OverflowError) as exc:
        raise invalid(f"{name} contains invalid coordinates.") from exc
    if not in_usa(lon, lat):
        raise PlanningError("location_outside_usa", f"{name} must be inside the USA.")
    return Endpoint(lon, lat, f"{lat:.6f}, {lon:.6f}")


def parse_request(payload):
    allowed = {
        "start",
        "finish",
        "initial_fuel_gallons",
        "initial_fuel_price_usd_per_gallon",
        "verify_stops",
        "station_corridor_miles",
    }
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise invalid("Body must be an object containing only documented request fields.")
    if not {"start", "finish"} <= set(payload):
        raise invalid("Both start and finish are required.")
    verify = payload.get("verify_stops", settings.DEFAULT_VERIFY_STOPS)
    if not isinstance(verify, bool):
        raise invalid("verify_stops must be a JSON boolean.")
    options = TripOptions(
        decimal_option(
            payload.get("initial_fuel_gallons", settings.DEFAULT_INITIAL_FUEL_GALLONS),
            "initial_fuel_gallons",
            CAPACITY_GALLONS,
            7,
            False,
        ),
        decimal_option(
            payload.get(
                "initial_fuel_price_usd_per_gallon", settings.DEFAULT_INITIAL_FUEL_PRICE_USD
            ),
            "initial_fuel_price_usd_per_gallon",
            Decimal(1000),
            8,
        ),
        verify,
        decimal_option(
            payload.get("station_corridor_miles", settings.DEFAULT_STATION_CORRIDOR_MILES),
            "station_corridor_miles",
            Decimal(20),
            6,
            False,
        ),
    )
    return TripRequest(
        parse_location(payload["start"], "start"),
        parse_location(payload["finish"], "finish"),
        options,
    )
