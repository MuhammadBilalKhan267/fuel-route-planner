"""Exact greedy purchasing on a fixed, ordered, one-dimensional itinerary."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .errors import PlanningError

MAX_RANGE_MILES = Decimal(500)
MPG = Decimal(10)
CAPACITY_GALLONS = MAX_RANGE_MILES / MPG
MILE_PRECISION = Decimal("0.000001")
EPSILON = Decimal("0.00000001")
CENT = Decimal("0.01")


def miles(value):
    return Decimal(str(value)).quantize(MILE_PRECISION)


def money(value):
    return value.quantize(CENT, ROUND_HALF_UP)


@dataclass(frozen=True)
class Station:
    opis_id: int
    name: str
    price: Decimal
    latitude: float
    longitude: float
    address: str = ""
    city: str = ""
    state: str = ""
    coordinate_source: str = ""
    price_observations: int = 1
    opis_aliases: tuple[int, ...] = ()


@dataclass(frozen=True)
class LocatedStation:
    station: Station
    mile: Decimal
    deviation_miles: float = 0


@dataclass(frozen=True)
class Purchase:
    location: LocatedStation
    gallons: Decimal
    arrival_fuel: Decimal
    departure_fuel: Decimal

    @property
    def cost(self):
        return money(self.gallons * self.location.station.price)


@dataclass(frozen=True)
class FuelPlan:
    distance: Decimal
    initial_fuel: Decimal
    initial_price: Decimal
    purchases: tuple[Purchase, ...]
    ending_fuel: Decimal

    @property
    def initial_cost(self):
        return money(self.initial_fuel * self.initial_price)

    @property
    def purchase_cost(self):
        return sum((p.cost for p in self.purchases), Decimal("0.00"))

    @property
    def total_cost(self):
        return self.initial_cost + self.purchase_cost

    @property
    def gallons_purchased(self):
        return sum((p.gallons for p in self.purchases), Decimal(0))


def optimize(stations, distance, initial_fuel=CAPACITY_GALLONS, initial_price=Decimal(0)):
    distance = Decimal(str(distance))
    initial_fuel, initial_price = Decimal(str(initial_fuel)), Decimal(str(initial_price))
    if (
        not all(value.is_finite() for value in (distance, initial_fuel, initial_price))
        or distance < 0
        or not 0 < initial_fuel <= CAPACITY_GALLONS
        or initial_price < 0
    ):
        raise ValueError("Invalid distance or initial fuel configuration.")
    distance = miles(distance)

    # The cheapest station dominates other stations at the same route position.
    by_position = {}
    for item in stations:
        if (
            not item.mile.is_finite()
            or not item.station.price.is_finite()
            or item.station.price <= 0
        ):
            raise ValueError("Station positions must be finite and prices must be positive.")
        position = miles(item.mile)
        if not 0 <= position < distance:
            continue
        normalized = LocatedStation(item.station, position, item.deviation_miles)
        previous = by_position.get(position)
        if previous is None or (item.station.price, item.station.opis_id) < (
            previous.station.price,
            previous.station.opis_id,
        ):
            by_position[position] = normalized
    ordered = sorted(by_position.values(), key=lambda item: item.mile)

    # A terminal with price zero means 'buy enough to finish', not a fuel sale.
    prices = [item.station.price for item in ordered] + [Decimal(0)]
    positions = [item.mile for item in ordered] + [distance]
    next_cheaper = [None] * len(prices)
    stack = []
    for index in range(len(prices) - 1, -1, -1):
        while stack and prices[stack[-1]] >= prices[index]:
            stack.pop()
        if stack:
            next_cheaper[index] = stack[-1]
        stack.append(index)

    remaining, previous_mile = initial_fuel, Decimal(0)
    purchases = []
    for index, item in enumerate(ordered):
        remaining -= (item.mile - previous_mile) / MPG
        if remaining < -EPSILON:
            raise PlanningError(
                "fuel_plan_unavailable", "Available station data has no reachable fuel chain."
            )
        remaining = max(Decimal(0), remaining)
        cheaper = next_cheaper[index]
        target = CAPACITY_GALLONS
        if cheaper is not None and positions[cheaper] - item.mile <= MAX_RANGE_MILES:
            target = (positions[cheaper] - item.mile) / MPG
        quantity = max(Decimal(0), target - remaining)
        if quantity:
            purchases.append(Purchase(item, quantity, remaining, remaining + quantity))
            remaining += quantity
        previous_mile = item.mile

    remaining -= (distance - previous_mile) / MPG
    if remaining < -EPSILON:
        raise PlanningError(
            "fuel_plan_unavailable", "Available station data has no reachable fuel chain."
        )
    return FuelPlan(
        distance, initial_fuel, initial_price, tuple(purchases), max(remaining, Decimal(0))
    )
