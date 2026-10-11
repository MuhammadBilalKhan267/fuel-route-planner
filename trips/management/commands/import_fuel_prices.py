import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from trips.importing import import_prices


class Command(BaseCommand):
    help = "Import mean US station prices and optional saved coordinates, without network calls."

    def add_arguments(self, parser):
        parser.add_argument(
            "source",
            nargs="?",
            type=Path,
            default=settings.BASE_DIR / "fuel-prices-for-be-assessment.csv",
        )
        parser.add_argument("--coordinates", type=Path)
        parser.add_argument(
            "--replace-coordinates",
            action="store_true",
            help="Use the mapping as the complete coordinate set; clear coordinates omitted from it.",
        )

    def handle(self, *args, **options):
        result = import_prices(
            options["source"],
            options["coordinates"],
            replace_coordinates=options["replace_coordinates"],
        )
        self.stdout.write(json.dumps(result, indent=2))
