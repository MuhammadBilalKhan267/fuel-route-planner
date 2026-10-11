import csv
import io
import json
from pathlib import Path

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from trips.geometry import in_usa
from trips.importing import normalized_address, refresh_revision
from trips.models import FuelStation
from trips.provider import coordinate_pair

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"


class Command(BaseCommand):
    help = "Prepare a Census batch; --submit stores exact address matches as unverified search anchors."

    def add_arguments(self, parser):
        parser.add_argument("--submit", action="store_true")
        parser.add_argument("--limit", type=int, default=10_000)
        parser.add_argument(
            "--results", type=Path, help="Save the Census response for local address-match review."
        )
        parser.add_argument(
            "--output",
            type=Path,
            default=settings.BASE_DIR / "data" / "pending-station-addresses.csv",
        )

    def handle(self, *args, **options):
        if options["results"] and options["results"].exists():
            raise CommandError("Results file already exists. Choose a new filename to preserve it.")
        if not 1 <= options["limit"] <= 10_000:
            raise CommandError("Batch limit must be between 1 and 10000.")
        stations = list(
            FuelStation.objects.filter(active=True, latitude__isnull=True).order_by("opis_id")[
                : options["limit"]
            ]
        )
        if not stations:
            self.stdout.write("No unmapped active stations; no network call made.")
            return
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        for station in stations:
            writer.writerow([station.opis_id, station.address, station.city, station.state, ""])
        options["output"].parent.mkdir(parents=True, exist_ok=True)
        with options["output"].open("w", encoding="utf-8", newline="") as handle:
            handle.write(buffer.getvalue())
        if not options["submit"]:
            self.stdout.write(
                f"Prepared {len(stations)} addresses at {options['output']}; no network call made."
            )
            return
        try:
            response = httpx.post(
                CENSUS_URL,
                data={"benchmark": "Public_AR_Current"},
                files={"addressFile": ("stations.csv", buffer.getvalue().encode(), "text/csv")},
                timeout=120,
                follow_redirects=False,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CommandError(
                "Census geocoding failed; saved coordinates were not modified."
            ) from exc
        expected = {station.opis_id: station for station in stations}
        if options["results"]:
            options["results"].parent.mkdir(parents=True, exist_ok=True)
            with options["results"].open("x", encoding="utf-8", newline="") as handle:
                handle.write(response.text)
        matches = {}
        for row in csv.reader(io.StringIO(response.text)):
            if (
                len(row) < 6
                or row[2].strip().casefold() != "match"
                or row[3].strip().casefold() != "exact"
            ):
                continue
            try:
                identifier = int(row[0])
                lon, lat = coordinate_pair([float(value) for value in row[5].split(",")])
                if identifier not in expected or not in_usa(lon, lat):
                    continue
            except (ValueError, TypeError):
                continue
            station = expected[identifier]
            station.latitude, station.longitude = lat, lon
            station.coordinate_source = "census_address_anchor"
            station.coordinate_verified = False
            matches[identifier] = station
        with transaction.atomic():
            current = {
                station.opis_id: station
                for station in FuelStation.objects.select_for_update().filter(
                    opis_id__in=matches, active=True, latitude__isnull=True
                )
            }
            matches = {
                identifier: station
                for identifier, station in matches.items()
                if identifier in current
                and normalized_address(station.address, station.city, station.state)
                == normalized_address(
                    current[identifier].address, current[identifier].city, current[identifier].state
                )
            }
            FuelStation.objects.bulk_update(
                list(matches.values()),
                ["latitude", "longitude", "coordinate_source", "coordinate_verified"],
            )
            refresh_revision()
        self.stdout.write(
            json.dumps(
                {
                    "submitted": len(stations),
                    "exact_address_matches": len(matches),
                    "still_unmapped": FuelStation.objects.filter(
                        opis_id__in=expected, active=True, latitude__isnull=True
                    ).count(),
                }
            )
        )
