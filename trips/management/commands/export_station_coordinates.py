import csv
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from trips.models import FuelStation


class Command(BaseCommand):
    help = "Export persisted station coordinates for a reproducible offline import."

    def add_arguments(self, parser):
        parser.add_argument(
            "output",
            nargs="?",
            type=Path,
            default=settings.BASE_DIR / "data" / "station-coordinates.csv",
        )

    def handle(self, *args, **options):
        path = options["output"]
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = (
            FuelStation.objects.filter(active=True, latitude__isnull=False, longitude__isnull=False)
            .order_by("opis_id")
            .values_list(
                "opis_id",
                "latitude",
                "longitude",
                "coordinate_source",
                "address",
                "city",
                "state",
                "coordinate_verified",
                "operational_status",
                "site_id",
            )
        )
        count = 0
        try:
            with path.open("x", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    [
                        "opis_id",
                        "latitude",
                        "longitude",
                        "source",
                        "address",
                        "city",
                        "state",
                        "verified",
                        "operational_status",
                        "site_id",
                    ]
                )
                for row in rows.iterator():
                    writer.writerow(row)
                    count += 1
        except FileExistsError as exc:
            raise CommandError(
                "Output already exists. Choose a new filename to preserve the previous mapping."
            ) from exc
        self.stdout.write(f"Exported {count} station coordinates to {path}; no network call made.")
