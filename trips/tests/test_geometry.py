from decimal import Decimal
from unittest import TestCase

from trips.fuel import optimize
from trips.geometry import GEOD, METERS_PER_MILE, StationIndex, in_usa

from .helpers import located, route


class GeometryTests(TestCase):
    def test_us_boundary_accepts_all_three_regions_and_dc(self):
        for lon, lat in [(-97, 35), (-74, 40.7), (-149.9, 61.2), (-157.85, 21.3), (-77.04, 38.9)]:
            with self.subTest(lon=lon, lat=lat):
                self.assertTrue(in_usa(lon, lat))

    def test_us_boundary_excludes_canada_mexico_and_ocean(self):
        for lon, lat in [(-79.38, 43.65), (-99.13, 19.43), (-60, 30), (0, 0)]:
            with self.subTest(lon=lon, lat=lat):
                self.assertFalse(in_usa(lon, lat))

    def test_nearest_segment_interior_instead_of_nearest_vertex(self):
        value = route([[-98, 35], [-96, 35]], [200])
        station = located(1, 0, 3, -97, 35.005).station
        (match,) = StationIndex([station]).along_route(value, 2)
        self.assertAlmostEqual(float(match.mile), 100, delta=0.1)
        self.assertLess(match.deviation_miles, 1)

    def test_stations_are_sorted_by_route_progress(self):
        value = route([[-98, 35], [-96, 35]], [200])
        stations = [located(1, 0, 3, -96.5, 35).station, located(2, 0, 3, -97.5, 35).station]
        matches = StationIndex(stations).along_route(value, 2)
        self.assertEqual([s.station.opis_id for s in matches], [2, 1])

    def test_corridor_uses_miles_not_degrees(self):
        value = route([[-97, 34], [-97, 36]], [200])
        lon, lat, _ = GEOD.fwd(-97, 35, 90, 5 * METERS_PER_MILE)
        station = located(1, 0, 3, lon, lat).station
        index = StationIndex([station])
        self.assertEqual(index.along_route(value, 2), [])
        (match,) = index.along_route(value, 6)
        self.assertAlmostEqual(match.deviation_miles, 5, delta=0.05)

    def test_corner_uses_leg_distances_for_progress(self):
        value = route([[-98, 35], [-97, 35], [-97, 36]], [80, 120])
        (match,) = StationIndex([located(1, 0, 3, -97, 35.5).station]).along_route(value, 2)
        self.assertAlmostEqual(float(match.mile), 140, delta=0.2)

    def test_duplicate_geometry_vertices(self):
        value = route([[-98, 35], [-98, 35], [-96, 35]], [200], [0, 2])
        (match,) = StationIndex([located(1, 0, 3, -97, 35).station]).along_route(value, 2)
        self.assertAlmostEqual(float(match.mile), 100, delta=0.1)

    def test_empty_station_index(self):
        self.assertEqual(StationIndex([]).along_route(route([[-98, 35], [-96, 35]], [200]), 2), [])

    def test_zero_distance_route(self):
        self.assertEqual(
            StationIndex([located(1, 0, 3).station]).along_route(
                route([[-97, 35], [-97, 35]], [0]), 2
            ),
            [],
        )

    def test_distant_stations_are_filtered_before_projection(self):
        value = route([[-98, 35], [-96, 35]], [200])
        stations = [located(1, 0, 3, -97, 35).station] + [
            located(i, 0, 3, -120, 40 + i / 10000).station for i in range(2, 2000)
        ]
        matches = StationIndex(stations).along_route(value, 2)
        self.assertEqual([m.station.opis_id for m in matches], [1])

    def test_high_latitude_longitude_envelope_does_not_miss_nearby_station(self):
        value = route([[-150, 60], [-150, 62]], [150])
        lon, lat, _ = GEOD.fwd(-150, 61, 90, 1.5 * METERS_PER_MILE)
        (match,) = StationIndex([located(1, 0, 3, lon, lat).station]).along_route(value, 2)
        self.assertLess(match.deviation_miles, 2)

    def test_reversed_route_reverses_station_order(self):
        stations = [located(1, 0, 3, -97.5, 35).station, located(2, 0, 3, -96.5, 35).station]
        matches = StationIndex(stations).along_route(route([[-96, 35], [-98, 35]], [200]), 2)
        self.assertEqual([m.station.opis_id for m in matches], [2, 1])

    def test_retraced_segment_retains_each_station_visit(self):
        value = route([[-98, 35], [-96, 35], [-98, 35]], [200, 200])
        matches = StationIndex([located(1, 0, 3, -97, 35).station]).along_route(value, 2)
        self.assertEqual([match.mile for match in matches], [Decimal(100), Decimal(300)])

    def test_repeated_station_visits_enable_a_feasible_fuel_chain(self):
        value = route([[-98, 35], [-96, 35], [-98, 35], [-96, 35]], [400, 400, 400])
        matches = StationIndex([located(1, 0, 3, -97, 35).station]).along_route(value, 2)
        self.assertEqual(
            [match.mile for match in matches], [Decimal(200), Decimal(600), Decimal(1000)]
        )
        plan = optimize(matches, value.distance_miles)
        self.assertEqual([purchase.gallons for purchase in plan.purchases], [20, 40, 10])
        self.assertEqual(plan.total_cost, Decimal("210.00"))
        self.assertEqual(plan.ending_fuel, 0)
        self.assertEqual(
            plan.initial_fuel + plan.gallons_purchased - plan.distance / 10, plan.ending_fuel
        )

    def test_adjacent_segments_at_corner_share_one_station_visit(self):
        value = route([[-98, 35], [-97, 35], [-97, 36]], [80, 120])
        (match,) = StationIndex([located(1, 0, 3, -97, 35).station]).along_route(value, 2)
        self.assertEqual(match.mile, Decimal(80))

    def test_vertex_progress_preserves_provider_leg_totals(self):
        value = route([[-98, 35], [-97.5, 35], [-97, 35], [-96, 35]], [100, 150], [0, 2, 3])
        progress = value.vertex_miles()
        self.assertEqual(progress[0], 0)
        self.assertAlmostEqual(progress[1], 50, delta=0.01)
        self.assertEqual(progress[2:], [100, 250])
