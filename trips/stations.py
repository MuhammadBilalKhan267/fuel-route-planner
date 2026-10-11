from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache

from django.db.models.functions import Coalesce

from .fuel import Station
from .geometry import StationIndex
from .models import FuelStation, StationDataset


@lru_cache(maxsize=4)
def station_index(revision):
    rows = (
        FuelStation.objects.filter(
            active=True, coordinate_verified=True, latitude__isnull=False, longitude__isnull=False
        )
        .exclude(operational_status=FuelStation.OperationalStatus.CLOSED)
        .values(
            "opis_id",
            "name",
            "price",
            "latitude",
            "longitude",
            "address",
            "city",
            "state",
            "coordinate_source",
            "site_id",
            "price_observations",
        )
    )
    sites = defaultdict(list)
    for row in rows:
        sites[row["site_id"] or row["opis_id"]].append(row)
    stations = []
    for site_id, aliases in sites.items():
        representative = min(
            aliases, key=lambda item: (item["opis_id"] != site_id, item["opis_id"])
        )
        values = dict(representative)
        values.pop("site_id")
        count = sum(item["price_observations"] for item in aliases)
        values["price"] = (
            sum(item["price"] * item["price_observations"] for item in aliases) / count
        ).quantize(Decimal("0.00000001"), ROUND_HALF_UP)
        values["price_observations"] = count
        values["opis_aliases"] = tuple(sorted(item["opis_id"] for item in aliases))
        stations.append(Station(**values))
    return StationIndex(stations)


def dataset_status():
    dataset = StationDataset.objects.filter(pk=1).first()
    active = FuelStation.objects.filter(active=True)
    mapped = active.filter(coordinate_verified=True)
    return {
        "revision": dataset.revision if dataset else "uninitialized",
        "us_stations": active.count(),
        "mapped_stations": mapped.count(),
        "routable_stations": mapped.exclude(operational_status=FuelStation.OperationalStatus.CLOSED)
        .annotate(physical_site=Coalesce("site_id", "opis_id"))
        .values("physical_site")
        .distinct()
        .count(),
        "known_closed_stations": active.filter(
            operational_status=FuelStation.OperationalStatus.CLOSED
        ).count(),
    }
