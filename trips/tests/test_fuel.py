"""An exhaustive state-space oracle checks the greedy rule independently."""

import random
from decimal import Decimal
from unittest import TestCase

from trips.errors import PlanningError
from trips.fuel import optimize

from .helpers import located


def exhaustive_cost(stations, distance_gallons, initial_gallons):
    # State = gallons on arrival. Enumerate every possible purchase and retain
    # the cheapest cost for each departure state; no greedy rule is used.
    costs = {initial_gallons: 0}
    previous = 0
    for position, cents in [*stations, (distance_gallons, None)]:
        gap = position - previous
        arrival = {fuel - gap: cost for fuel, cost in costs.items() if fuel >= gap}
        if cents is None:
            return min(arrival.values(), default=None)
        costs = {}
        for fuel, cost in arrival.items():
            for departure in range(fuel, 51):
                candidate = cost + (departure - fuel) * cents
                costs[departure] = min(costs.get(departure, candidate), candidate)
        previous = position
    raise AssertionError("Missing destination")


class FuelTests(TestCase):
    def assert_valid(self, plan):
        self.assertEqual(
            plan.initial_fuel + plan.gallons_purchased - plan.distance / 10, plan.ending_fuel
        )
        previous, fuel = Decimal(0), plan.initial_fuel
        for purchase in plan.purchases:
            self.assertEqual(purchase.arrival_fuel, fuel - (purchase.location.mile - previous) / 10)
            self.assertGreater(purchase.gallons, 0)
            self.assertGreaterEqual(purchase.arrival_fuel, 0)
            self.assertLessEqual(purchase.departure_fuel, 50)
            self.assertEqual(purchase.departure_fuel, purchase.arrival_fuel + purchase.gallons)
            fuel, previous = purchase.departure_fuel, purchase.location.mile
        self.assertEqual(plan.purchase_cost, sum((p.cost for p in plan.purchases), Decimal(0)))

    def test_300_generated_cases_agree_with_exhaustive_oracle(self):
        randomizer = random.Random(20261010)
        feasible = infeasible = 0
        for case in range(300):
            distance = randomizer.randint(1, 160)
            positions = sorted(
                randomizer.sample(range(distance), min(randomizer.randint(0, 10), distance))
            )
            stations = [(p, randomizer.randint(1, 9) * 100) for p in positions]
            initial = randomizer.randint(1, 50)
            expected = exhaustive_cost(stations, distance, initial)
            inputs = [
                located(i + 1, p * 10, Decimal(price) / 100)
                for i, (p, price) in enumerate(stations)
            ]
            randomizer.shuffle(inputs)
            with self.subTest(case=case, stations=stations, distance=distance, initial=initial):
                if expected is None:
                    infeasible += 1
                    with self.assertRaises(PlanningError):
                        optimize(inputs, distance * 10, initial)
                else:
                    feasible += 1
                    plan = optimize(inputs, distance * 10, initial)
                    self.assertEqual(plan.purchase_cost, Decimal(expected) / 100)
                    self.assert_valid(plan)
        self.assertGreater(feasible, 40)
        self.assertGreater(infeasible, 40)

    def test_short_trip_spends_nothing_from_free_initial_tank(self):
        plan = optimize([located(1, 100, 1)], 400)
        self.assertEqual(plan.total_cost, 0)
        self.assertEqual(plan.ending_fuel, 10)
        self.assertEqual(plan.purchases, ())

    def test_exact_range_without_stations(self):
        plan = optimize([], 500)
        self.assertEqual(plan.ending_fuel, 0)
        self.assert_valid(plan)

    def test_one_millionth_mile_beyond_range_fails(self):
        with self.assertRaises(PlanningError):
            optimize([], "500.000001")

    def test_zero_distance_preserves_initial_inventory(self):
        plan = optimize([], 0, 12, "2.5")
        self.assertEqual(plan.ending_fuel, 12)
        self.assertEqual(plan.total_cost, 30)

    def test_multiple_full_tank_stops(self):
        plan = optimize([located(1, 500, 3), located(2, 1000, 4)], 1400)
        self.assertEqual([p.gallons for p in plan.purchases], [50, 40])
        self.assertEqual(plan.purchase_cost, 310)
        self.assert_valid(plan)

    def test_buy_only_enough_to_reach_cheaper_station(self):
        plan = optimize([located(1, 400, 4), located(2, 600, 2)], 900)
        self.assertEqual([p.gallons for p in plan.purchases], [10, 30])
        self.assertEqual(plan.purchase_cost, 100)
        self.assert_valid(plan)

    def test_fill_at_cheaper_station_before_more_expensive_one(self):
        plan = optimize([located(1, 400, 2), located(2, 700, 4)], 1100)
        self.assertEqual([p.gallons for p in plan.purchases], [40, 20])
        self.assertEqual(plan.purchase_cost, 160)
        self.assert_valid(plan)

    def test_cheaper_station_outside_range_cannot_justify_underfilling(self):
        plan = optimize([located(1, 400, 4), located(2, 800, 5), located(3, 901, 1)], 1200)
        self.assertEqual(plan.purchases[0].departure_fuel, 50)
        self.assert_valid(plan)

    def test_partial_initial_tank(self):
        plan = optimize([located(1, 100, 3)], 300, 10)
        self.assertEqual(plan.purchases[0].arrival_fuel, 0)
        self.assertEqual(plan.purchases[0].gallons, 20)
        self.assert_valid(plan)

    def test_station_at_start_can_supply_partial_tank(self):
        plan = optimize([located(1, 0, 3)], 400, 1)
        self.assertEqual(plan.gallons_purchased, 39)

    def test_price_of_owned_fuel_changes_total_but_not_purchases(self):
        stations = [located(1, 400, 3)]
        free, valued = optimize(stations, 800), optimize(stations, 800, 50, "2.4")
        self.assertEqual(free.purchases, valued.purchases)
        self.assertEqual(valued.initial_cost, 120)
        self.assertEqual(valued.total_cost - free.total_cost, 120)

    def test_duplicate_positions_choose_cheapest_then_lowest_id(self):
        plan = optimize([located(3, 400, 2), located(1, 400, 2), located(2, 400, 5)], 800)
        self.assertEqual([p.location.station.opis_id for p in plan.purchases], [1])

    def test_equal_prices_preserve_minimum_cost(self):
        plan = optimize([located(1, 300, 3), located(2, 600, 3)], 900)
        self.assertEqual(plan.purchase_cost, 120)
        self.assert_valid(plan)

    def test_fractional_fuel_and_price(self):
        plan = optimize([located(1, "100.125", "3.12345678")], "150.625", "10.0125")
        self.assertEqual(plan.gallons_purchased, Decimal("5.050000"))
        self.assertEqual(plan.purchase_cost, Decimal("15.77"))
        self.assert_valid(plan)

    def test_money_rounding_is_half_up_and_total_reconciles(self):
        plan = optimize([located(1, 10, "1.005"), located(2, 20, "1.005")], 30, 1, "0.005")
        self.assertEqual(plan.initial_cost, Decimal("0.01"))
        self.assertEqual(
            plan.total_cost, plan.initial_cost + sum((p.cost for p in plan.purchases), Decimal(0))
        )

    def test_destination_station_does_not_enable_an_unreachable_trip(self):
        with self.assertRaises(PlanningError):
            optimize([located(1, 600, 1)], 600)

    def test_stations_outside_itinerary_are_ignored(self):
        plan = optimize([located(1, -1, 3), located(2, 501, 3)], 500)
        self.assertEqual(plan.purchases, ())

    def test_unreachable_first_station(self):
        with self.assertRaises(PlanningError):
            optimize([located(1, 101, 3)], 200, 10)

    def test_gap_over_500_between_stations(self):
        with self.assertRaises(PlanningError):
            optimize([located(1, 500, 3), located(2, 1001, 4)], 1400)

    def test_invalid_initial_configuration(self):
        for distance, fuel, price in [
            (-1, 50, 0),
            (1, 0, 0),
            (1, -1, 0),
            (1, 51, 0),
            (1, 50, -1),
            ("NaN", 50, 0),
            (1, "Infinity", 0),
            (1, 50, "NaN"),
        ]:
            with (
                self.subTest(distance=distance, fuel=fuel, price=price),
                self.assertRaises(ValueError),
            ):
                optimize([], distance, fuel, price)

    def test_invalid_station_price_or_position(self):
        for position, price in [(10, 0), (10, -1), (10, "NaN"), ("Infinity", 3)]:
            with self.subTest(position=position, price=price), self.assertRaises(ValueError):
                optimize([located(1, position, price)], 100)
