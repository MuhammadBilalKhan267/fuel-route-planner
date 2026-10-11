import csv
import io
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError

from trips.importing import import_prices, read_prices, refresh_revision
from trips.management.commands.geocode_stations import CENSUS_URL
from trips.models import FuelStation, StationDataset
from trips.stations import station_index

from .helpers import CachedTestCase, create_station

HEADERS = ["OPIS Truckstop ID", "Truckstop Name", "Address", "City", "State", "Retail Price"]


class ImportTests(CachedTestCase):
    def setUp(self):
        super().setUp()
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "prices.csv"
        self.mapping = Path(self.directory.name) / "coordinates.csv"

    def tearDown(self):
        self.directory.cleanup()
        super().tearDown()

    def prices(self, rows, headers=HEADERS):
        with self.path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
        return self.path

    def coordinates(self, rows, headers=None):
        with self.mapping.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers or ["opis_id", "latitude", "longitude", "source"])
            writer.writerows(rows)
        return self.mapping

    def row(self, identifier=1, price="3", address="1 Main St", state="OK"):
        return [identifier, f"Station {identifier}", address, "Oklahoma City", state, price]

    def test_mean_uses_all_rows_and_decimal_arithmetic(self):
        import_prices(
            self.prices([self.row(price="3.1"), self.row(price="3.1"), self.row(price="6.1")])
        )
        value = FuelStation.objects.get(opis_id=1)
        self.assertEqual(value.price, Decimal("4.10000000"))
        self.assertEqual(value.price_observations, 3)

    def test_csv_bom_and_quoted_commas(self):
        import_prices(self.prices([self.row(address="1 Main St, Unit 2")]))
        self.assertEqual(FuelStation.objects.get().address, "1 Main St, Unit 2")

    def test_canadian_rows_are_excluded(self):
        result = import_prices(self.prices([self.row(), self.row(2, state="ON")]))
        self.assertEqual(result["input_rows"], 2)
        self.assertEqual(result["non_us_rows"], 1)
        self.assertEqual(result["us_stations"], 1)
        self.assertEqual(FuelStation.objects.count(), 1)

    def test_supplied_dataset_counts_and_example_mean(self):
        values, counts = read_prices(settings.BASE_DIR / "fuel-prices-for-be-assessment.csv")
        self.assertEqual(counts, {"input_rows": 8151, "non_us_rows": 620})
        self.assertEqual(len(values), 6626)
        self.assertEqual(values[105]["price_observations"], 6)
        self.assertEqual(values[105]["price"], Decimal("3.33733333"))

    def test_missing_headers(self):
        with self.assertRaises(CommandError):
            import_prices(self.prices([], ["Price"]))

    def test_invalid_row_rolls_back_entire_import(self):
        create_station(price="8")
        original = StationDataset.objects.get(pk=1).revision
        with self.assertRaises(CommandError):
            import_prices(self.prices([self.row(price="2"), self.row(2, price="NaN")]))
        self.assertEqual(FuelStation.objects.get(opis_id=1).price, 8)
        self.assertEqual(StationDataset.objects.get(pk=1).revision, original)
        self.assertEqual(FuelStation.objects.count(), 1)

    def test_bad_price_id_and_missing_address(self):
        for row in [
            self.row(price="0"),
            self.row(price="-1"),
            self.row(price="Infinity"),
            self.row(price="abc"),
            self.row(price="0.00000000001"),
            self.row(0),
            self.row(address=""),
        ]:
            with self.subTest(row=row), self.assertRaises(CommandError):
                import_prices(self.prices([row]))
        self.assertEqual(FuelStation.objects.count(), 0)

    def test_conflicting_addresses_for_same_id_are_rejected(self):
        with self.assertRaises(CommandError):
            import_prices(self.prices([self.row(), self.row(address="2 Main St")]))

    def test_address_case_and_spacing_are_normalized(self):
        result = import_prices(
            self.prices([self.row(address="1 MAIN  ST"), self.row(address="1 Main St")])
        )
        self.assertEqual(result["us_stations"], 1)

    def test_empty_or_non_us_import_preserves_existing_data(self):
        create_station()
        for rows in [[], [self.row(state="ON")]]:
            with self.subTest(rows=rows), self.assertRaises(CommandError):
                import_prices(self.prices(rows))
        self.assertTrue(FuelStation.objects.get().active)

    def test_station_coordinate_mapping_is_persisted(self):
        mapping = self.coordinates(
            [[1, 35, -97, "surveyed_station", "true"]],
            ["opis_id", "latitude", "longitude", "source", "verified"],
        )
        result = import_prices(self.prices([self.row()]), mapping)
        value = FuelStation.objects.get()
        self.assertEqual(
            (value.latitude, value.longitude, value.coordinate_source),
            (35, -97, "surveyed_station"),
        )
        self.assertEqual(result["mapped_stations"], 1)

    def test_price_reimport_preserves_coordinates_without_network(self):
        create_station()
        with patch("httpx.post") as http:
            import_prices(self.prices([self.row(price="4")]))
        value = FuelStation.objects.get()
        self.assertEqual(value.price, 4)
        self.assertEqual((value.latitude, value.longitude), (35, -97))
        http.assert_not_called()

    def test_authoritative_coordinate_import_retracts_stale_verified_locations(self):
        create_station(1, site_id=1)
        create_station(2, site_id=1)
        previous_revision = StationDataset.objects.get(pk=1).revision
        mapping = self.coordinates(
            [[1, 35, -97, "reviewed_station", "true"]],
            ["opis_id", "latitude", "longitude", "source", "verified"],
        )
        result = import_prices(
            self.prices([self.row(), self.row(2)]), mapping, replace_coordinates=True
        )
        removed = FuelStation.objects.get(opis_id=2)
        self.assertFalse(removed.coordinate_verified)
        self.assertIsNone(removed.latitude)
        self.assertIsNone(removed.longitude)
        self.assertIsNone(removed.site_id)
        self.assertEqual(removed.coordinate_source, "")
        self.assertEqual(result["mapped_stations"], 1)
        self.assertNotEqual(result["revision"], previous_revision)
        self.assertEqual(len(station_index(result["revision"]).stations), 1)

    def test_coordinate_replacement_requires_a_mapping_and_rolls_back_on_bad_binding(self):
        create_station(price="8")
        with self.assertRaises(CommandError):
            import_prices(self.prices([self.row(price="2")]), replace_coordinates=True)
        mapping = self.coordinates(
            [[1, 35, -97, "reviewed_station", "2 Main St", "Oklahoma City", "OK"]],
            ["opis_id", "latitude", "longitude", "source", "address", "city", "state"],
        )
        with self.assertRaises(CommandError):
            import_prices(self.path, mapping, replace_coordinates=True)
        value = FuelStation.objects.get()
        self.assertEqual(value.price, 8)
        self.assertTrue(value.coordinate_verified)

    def test_coordinate_mapping_without_verification_is_an_anchor_only(self):
        result = import_prices(
            self.prices([self.row()]), self.coordinates([[1, 35, -97, "manual_candidate"]])
        )
        self.assertEqual(result["mapped_stations"], 0)
        self.assertFalse(FuelStation.objects.get().coordinate_verified)

    def test_intersection_mapping_cannot_claim_station_verification(self):
        for source in ("census_exact_address", "road_intersection", "city_search_anchor"):
            with self.subTest(source=source), self.assertRaises(CommandError):
                import_prices(
                    self.prices([self.row()]),
                    self.coordinates(
                        [[1, 35, -97, source, "true"]],
                        ["opis_id", "latitude", "longitude", "source", "verified"],
                    ),
                )

    def test_verification_and_closure_survive_price_only_reimport(self):
        create_station(operational_status="closed")
        import_prices(self.prices([self.row()]))
        value = FuelStation.objects.get()
        self.assertTrue(value.coordinate_verified)
        self.assertEqual(value.operational_status, "closed")

    def test_changed_station_address_clears_stale_coordinates(self):
        create_station()
        import_prices(self.prices([self.row(address="2 Main St")]))
        value = FuelStation.objects.get()
        self.assertIsNone(value.latitude)
        self.assertIsNone(value.longitude)
        self.assertEqual(value.coordinate_source, "")

    def test_changed_canonical_address_invalidates_preserved_physical_aliases(self):
        create_station(1, site_id=1, price="2")
        create_station(2, site_id=1, price="6")
        source = self.prices([self.row(address="2 Main St", price="2"), self.row(2, price="6")])
        import_prices(source)
        alias = FuelStation.objects.get(opis_id=2)
        self.assertIsNone(alias.site_id)
        self.assertTrue(alias.coordinate_verified)
        self.assertEqual((alias.latitude, alias.longitude), (35, -97))

        # Remapping the changed ID must not merge its new premises with the
        # unchanged alias that still occupies the original premises.
        mapping = self.coordinates(
            [[1, 35, -98, "surveyed_station", "true"]],
            ["opis_id", "latitude", "longitude", "source", "verified"],
        )
        result = import_prices(source, mapping)
        stations = station_index(result["revision"]).stations
        self.assertEqual(len(stations), 2)
        self.assertEqual({station.opis_id: station.price for station in stations}, {1: 2, 2: 6})

    def test_changed_noncanonical_alias_keeps_the_unchanged_physical_group(self):
        create_station(1, site_id=1)
        create_station(2, site_id=1)
        create_station(3, site_id=1)
        result = import_prices(
            self.prices([self.row(), self.row(2, address="2 Main St"), self.row(3)])
        )
        changed = FuelStation.objects.get(opis_id=2)
        self.assertIsNone(changed.site_id)
        self.assertFalse(changed.coordinate_verified)
        (station,) = station_index(result["revision"]).stations
        self.assertEqual(station.opis_aliases, (1, 3))

    def test_mapping_for_different_address_rolls_back(self):
        create_station(price="8")
        mapping = self.coordinates(
            [[1, 35, -97, "surveyed_station", "2 Main St", "Oklahoma City", "OK"]],
            ["opis_id", "latitude", "longitude", "source", "address", "city", "state"],
        )
        with self.assertRaises(CommandError):
            import_prices(self.prices([self.row(price="2")]), mapping)
        self.assertEqual(FuelStation.objects.get().price, 8)

    def test_mapping_requires_valid_us_coordinates_and_station_source(self):
        for lat, lon, source in [
            (43.65, -79.38, "surveyed_station"),
            (35, -97, "city_centroid"),
            (35, -97, ""),
            ("NaN", -97, "surveyed_station"),
            (95, -97, "surveyed_station"),
        ]:
            with self.subTest(lat=lat, lon=lon, source=source), self.assertRaises(CommandError):
                import_prices(self.prices([self.row()]), self.coordinates([[1, lat, lon, source]]))
        self.assertEqual(FuelStation.objects.count(), 0)

    def test_partial_address_binding_is_rejected_without_changing_data(self):
        create_station(price="8")
        for suffix, values in [
            (["address"], ["2 Main St"]),
            (["address", "city", "state"], ["2 Main St", "Oklahoma City", ""]),
            (["address", "city", "state"], ["", "", ""]),
        ]:
            with self.subTest(suffix=suffix, values=values):
                mapping = self.coordinates(
                    [[1, 35, -97, "surveyed_station", *values]],
                    ["opis_id", "latitude", "longitude", "source", *suffix],
                )
                with self.assertRaises(CommandError):
                    import_prices(self.prices([self.row(price="2")]), mapping)
                self.assertEqual(FuelStation.objects.get().price, 8)

    def test_repeated_coordinate_ids_are_rejected(self):
        with self.assertRaises(CommandError):
            import_prices(
                self.prices([self.row()]),
                self.coordinates([[1, 35, -97, "surveyed"], [1, 35, -97, "surveyed"]]),
            )

    def test_unknown_station_or_canonical_site_id_rejects_mapping(self):
        for identifier, site_id in ((99, ""), (1, "99")):
            with (
                self.subTest(identifier=identifier, site_id=site_id),
                self.assertRaises(CommandError),
            ):
                import_prices(
                    self.prices([self.row()]),
                    self.coordinates(
                        [[identifier, 35, -97, "surveyed_station", site_id]],
                        ["opis_id", "latitude", "longitude", "source", "site_id"],
                    ),
                )

    def test_obsolete_stations_are_deactivated_and_preserved(self):
        create_station(2)
        import_prices(self.prices([self.row()]))
        self.assertFalse(FuelStation.objects.get(opis_id=2).active)
        self.assertTrue(FuelStation.objects.get(opis_id=1).active)
        self.assertEqual(FuelStation.objects.count(), 2)

    def test_dataset_revision_invalidates_spatial_index_after_price_change(self):
        create_station()
        old = station_index(StationDataset.objects.get(pk=1).revision)
        result = import_prices(self.prices([self.row(price="5")]))
        new = station_index(result["revision"])
        self.assertIsNot(old, new)
        self.assertEqual(new.stations[0].price, 5)
        self.assertEqual(old.stations[0].price, 3)

    def test_unchanged_import_has_stable_revision(self):
        source = self.prices([self.row()])
        first = import_prices(source)
        second = import_prices(source)
        self.assertEqual(first["revision"], second["revision"])

    def test_import_command_returns_counts(self):
        output = io.StringIO()
        call_command("import_fuel_prices", str(self.prices([self.row()])), stdout=output)
        self.assertEqual(json.loads(output.getvalue())["us_stations"], 1)

    def test_geocoding_default_only_prepares_local_batch(self):
        create_station(latitude=None, longitude=None)
        create_station(2)
        output = Path(self.directory.name) / "batch.csv"
        with patch("trips.management.commands.geocode_stations.httpx.post") as post:
            call_command("geocode_stations", output=output, stdout=io.StringIO())
        post.assert_not_called()
        with output.open(newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows, [["1", "1 Main St", "Oklahoma City", "OK", ""]])

    def census_csv(self, rows):
        output = io.StringIO(newline="")
        csv.writer(output).writerows(rows)
        return output.getvalue()

    def test_submitted_batch_saves_only_exact_us_matches(self):
        for identifier in range(1, 5):
            create_station(identifier, latitude=None, longitude=None)
        text = self.census_csv(
            [
                [1, "input", "Match", "Exact", "matched address", "-97,35", "id", "L"],
                [2, "input", "Match", "Non_Exact", "matched address", "-97,35"],
                [3, "input", "No_Match"],
                [4, "input", "Match", "Exact", "matched address", "-79.38,43.65"],
            ]
        )
        response = httpx.Response(200, text=text, request=httpx.Request("POST", CENSUS_URL))
        with patch(
            "trips.management.commands.geocode_stations.httpx.post", return_value=response
        ) as post:
            call_command(
                "geocode_stations",
                submit=True,
                output=Path(self.directory.name) / "batch.csv",
                stdout=io.StringIO(),
            )
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args, (CENSUS_URL,))
        self.assertEqual(post.call_args.kwargs["data"], {"benchmark": "Public_AR_Current"})
        self.assertEqual(FuelStation.objects.filter(latitude__isnull=False).count(), 1)
        self.assertEqual(
            FuelStation.objects.get(opis_id=1).coordinate_source, "census_address_anchor"
        )

    def test_second_geocoding_run_skips_persisted_coordinates(self):
        create_station()
        with patch("trips.management.commands.geocode_stations.httpx.post") as post:
            call_command(
                "geocode_stations",
                submit=True,
                output=Path(self.directory.name) / "batch.csv",
                stdout=io.StringIO(),
            )
        post.assert_not_called()

    def test_census_failure_does_not_modify_saved_data(self):
        create_station(latitude=None, longitude=None)
        revision = refresh_revision()
        with (
            patch(
                "trips.management.commands.geocode_stations.httpx.post",
                side_effect=httpx.ReadTimeout("test"),
            ),
            self.assertRaises(CommandError),
        ):
            call_command(
                "geocode_stations",
                submit=True,
                output=Path(self.directory.name) / "batch.csv",
                stdout=io.StringIO(),
            )
        self.assertIsNone(FuelStation.objects.get().latitude)
        self.assertEqual(StationDataset.objects.get(pk=1).revision, revision)

    def test_coordinate_export_reimports_without_geocoding(self):
        create_station()
        output = Path(self.directory.name) / "export.csv"
        call_command("export_station_coordinates", str(output), stdout=io.StringIO())
        FuelStation.objects.update(
            latitude=None, longitude=None, coordinate_source="", coordinate_verified=False
        )
        with patch("httpx.post") as http:
            import_prices(self.prices([self.row()]), output)
        value = FuelStation.objects.get()
        self.assertEqual(
            (value.latitude, value.longitude, value.coordinate_source),
            (35, -97, "test_address_match"),
        )
        http.assert_not_called()

    def test_coordinate_export_preserves_existing_file(self):
        output = Path(self.directory.name) / "export.csv"
        output.write_text("valuable old mapping", encoding="utf-8")
        with self.assertRaises(CommandError):
            call_command("export_station_coordinates", str(output), stdout=io.StringIO())
        self.assertEqual(output.read_text(), "valuable old mapping")

    def test_census_result_is_not_attached_to_an_address_changed_in_flight(self):
        create_station(latitude=None, longitude=None)
        text = self.census_csv(
            [[1, "input", "Match", "Exact", "1 Main St, Oklahoma City, OK", "-97,35"]]
        )

        def changed_address(*args, **kwargs):
            FuelStation.objects.filter(opis_id=1).update(address="2 Main St")
            return httpx.Response(200, text=text, request=httpx.Request("POST", CENSUS_URL))

        with patch(
            "trips.management.commands.geocode_stations.httpx.post", side_effect=changed_address
        ):
            call_command(
                "geocode_stations",
                submit=True,
                output=Path(self.directory.name) / "batch.csv",
                stdout=io.StringIO(),
            )
        value = FuelStation.objects.get()
        self.assertIsNone(value.latitude)
        self.assertEqual(value.address, "2 Main St")

    def test_existing_mapping_created_in_flight_is_preserved(self):
        create_station(latitude=None, longitude=None)
        text = self.census_csv(
            [[1, "input", "Match", "Exact", "1 Main St, Oklahoma City, OK", "-97,35"]]
        )

        def concurrent_mapping(*args, **kwargs):
            FuelStation.objects.filter(opis_id=1).update(
                latitude=35.1, longitude=-97.1, coordinate_source="surveyed_station"
            )
            return httpx.Response(200, text=text, request=httpx.Request("POST", CENSUS_URL))

        output = io.StringIO()
        with patch(
            "trips.management.commands.geocode_stations.httpx.post", side_effect=concurrent_mapping
        ):
            call_command(
                "geocode_stations",
                submit=True,
                output=Path(self.directory.name) / "batch.csv",
                stdout=output,
            )
        value = FuelStation.objects.get()
        self.assertEqual(value.coordinate_source, "surveyed_station")
        self.assertEqual(value.latitude, 35.1)
        self.assertEqual(json.loads(output.getvalue())["still_unmapped"], 0)

    def test_census_diagnostics_are_saved_and_not_overwritten(self):
        create_station(latitude=None, longitude=None)
        output = Path(self.directory.name) / "results.csv"
        response = httpx.Response(
            200,
            text=self.census_csv([[1, "input", "No_Match"]]),
            request=httpx.Request("POST", CENSUS_URL),
        )
        with patch(
            "trips.management.commands.geocode_stations.httpx.post", return_value=response
        ) as post:
            call_command(
                "geocode_stations",
                submit=True,
                output=Path(self.directory.name) / "batch.csv",
                results=output,
                stdout=io.StringIO(),
            )
            with self.assertRaises(CommandError):
                call_command(
                    "geocode_stations",
                    submit=True,
                    output=Path(self.directory.name) / "batch.csv",
                    results=output,
                    stdout=io.StringIO(),
                )
        self.assertEqual(post.call_count, 1)
        self.assertIn("No_Match", output.read_text())

    def test_census_batch_limit(self):
        for limit in [0, 10001]:
            with self.subTest(limit=limit), self.assertRaises(CommandError):
                call_command("geocode_stations", limit=limit, stdout=io.StringIO())
