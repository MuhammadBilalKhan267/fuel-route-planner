"""CSV ingestion and reusable station-coordinate mapping."""

import csv
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import CommandError
from django.db import transaction

from .geometry import in_usa
from .models import FuelStation, StationDataset
from .provider import coordinate_pair

US_STATES = frozenset(
    [
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
    ]
)
SOURCE_HEADERS = {"OPIS Truckstop ID", "Truckstop Name", "Address", "City", "State", "Retail Price"}


@dataclass(frozen=True)
class CoordinateMapping:
    latitude: float
    longitude: float
    source: str
    binding: tuple | None
    verified: bool
    operational_status: str
    site_id: int | None


def normalized_address(address, city, state):
    return tuple(" ".join(value.casefold().split()) for value in (address, city, state))


def read_prices(source):
    grouped = defaultdict(list)
    counts = {"input_rows": 0, "non_us_rows": 0}
    with Path(source).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not SOURCE_HEADERS <= set(reader.fieldnames or []):
            raise CommandError("Price CSV is missing required headers.")
        for line, row in enumerate(reader, 2):
            counts["input_rows"] += 1
            state = (row.get("State") or "").strip().upper()
            if state not in US_STATES:
                counts["non_us_rows"] += 1
                continue
            try:
                identifier = int(row["OPIS Truckstop ID"])
                price = Decimal(row["Retail Price"])
                fields = {
                    "name": row["Truckstop Name"].strip(),
                    "address": row["Address"].strip(),
                    "city": row["City"].strip(),
                    "state": state,
                }
                if (
                    identifier <= 0
                    or not price.is_finite()
                    or not 0 < price < 100_000
                    or not all(fields.values())
                ):
                    raise ValueError("Invalid ID, price or address.")
                if (
                    len(fields["name"]) > 255
                    or len(fields["address"]) > 255
                    or len(fields["city"]) > 120
                ):
                    raise ValueError("Source field exceeds its storage limit.")
            except (ValueError, InvalidOperation, KeyError, AttributeError, TypeError) as exc:
                raise CommandError(f"Invalid US station data at CSV line {line}.") from exc
            grouped[identifier].append((fields, price))

    stations = {}
    for identifier, entries in grouped.items():
        fields = entries[0][0]
        if any(
            normalized_address(item["address"], item["city"], item["state"])
            != normalized_address(fields["address"], fields["city"], fields["state"])
            for item, _ in entries
        ):
            raise CommandError(
                f"Station {identifier} has conflicting addresses; resolve them before importing."
            )
        mean = (sum((price for _, price in entries), Decimal(0)) / len(entries)).quantize(
            Decimal("0.00000001"), ROUND_HALF_UP
        )
        if mean <= 0:
            raise CommandError(f"Station {identifier} price is below supported precision.")
        stations[identifier] = {**fields, "price": mean, "price_observations": len(entries)}
    return stations, counts


def read_coordinates(path):
    if path is None:
        return {}
    points = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not {"opis_id", "latitude", "longitude", "source"} <= set(reader.fieldnames or []):
            raise CommandError("Coordinate CSV requires opis_id, latitude, longitude and source.")
        address_fields = {"address", "city", "state"}
        binding_fields = address_fields & set(reader.fieldnames)
        if binding_fields and binding_fields != address_fields:
            raise CommandError(
                "Coordinate address binding requires address, city and state together."
            )
        for line, row in enumerate(reader, 2):
            try:
                identifier = int(row["opis_id"])
                lon, lat = coordinate_pair([float(row["longitude"]), float(row["latitude"])])
                source = row["source"].strip()
                if (
                    identifier <= 0
                    or not source
                    or len(source) > 120
                    or "centroid" in source.casefold()
                    or not in_usa(lon, lat)
                ):
                    raise ValueError("Coordinates require a US station-level source.")
                binding = (
                    normalized_address(row["address"], row["city"], row["state"])
                    if binding_fields
                    else None
                )
                if binding is not None and not all(binding):
                    raise ValueError("Coordinate address binding must contain all three values.")
                verified = (row.get("verified") or "false").strip().casefold()
                status = (row.get("operational_status") or "unknown").strip().casefold()
                if (
                    verified not in {"true", "false"}
                    or status not in FuelStation.OperationalStatus.values
                ):
                    raise ValueError("Invalid verification flag or operational status.")
                if verified == "true" and any(
                    term in source.casefold() for term in ("census", "anchor", "intersection")
                ):
                    raise ValueError(
                        "Address-range and intersection matches require station review."
                    )
                site_id = int(row["site_id"]) if row.get("site_id", "").strip() else None
                if site_id is not None and site_id <= 0:
                    raise ValueError("Physical site ID must be positive.")
                point = CoordinateMapping(
                    lat, lon, source, binding, verified == "true", status, site_id
                )
                if identifier in points:
                    raise ValueError("Repeated coordinate station ID.")
                points[identifier] = point
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
                raise CommandError(
                    f"Invalid or approximate coordinate mapping at line {line}."
                ) from exc
    return points


def refresh_revision(counts=None):
    rows = list(FuelStation.objects.filter(active=True).order_by("opis_id").values())
    digest = hashlib.sha256(
        json.dumps(rows, default=str, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    defaults = {"revision": digest}
    if counts is not None:
        defaults.update(source_rows=counts["input_rows"], non_us_rows=counts["non_us_rows"])
    StationDataset.objects.update_or_create(pk=1, defaults=defaults)
    return digest


def import_prices(source, coordinate_path=None, *, replace_coordinates=False):
    if replace_coordinates and coordinate_path is None:
        raise CommandError("Replacing coordinates requires a coordinate mapping file.")
    rows, counts = read_prices(source)
    if not rows:
        raise CommandError("Price CSV contains no US stations; existing data was not modified.")
    points = read_coordinates(coordinate_path)
    if points.keys() - rows.keys():
        raise CommandError("Coordinate mapping contains IDs absent from the US price dataset.")
    if any(point.site_id is not None and point.site_id not in rows for point in points.values()):
        raise CommandError("Physical site ID is absent from the US price dataset.")
    with transaction.atomic():
        existing = {
            station.opis_id: station for station in FuelStation.objects.filter(opis_id__in=rows)
        }
        changed_addresses = {
            identifier
            for identifier, previous in existing.items()
            if normalized_address(previous.address, previous.city, previous.state)
            != normalized_address(
                rows[identifier]["address"], rows[identifier]["city"], rows[identifier]["state"]
            )
        }
        records = []
        for identifier, fields in rows.items():
            previous = existing.get(identifier)
            same_address = previous is not None and identifier not in changed_addresses
            preserve_coordinates = same_address and not replace_coordinates
            latitude = previous.latitude if preserve_coordinates else None
            longitude = previous.longitude if preserve_coordinates else None
            source_label = previous.coordinate_source if preserve_coordinates else ""
            verified = previous.coordinate_verified if preserve_coordinates else False
            status = (
                previous.operational_status
                if preserve_coordinates
                else FuelStation.OperationalStatus.UNKNOWN
            )
            site_id = previous.site_id if preserve_coordinates else None
            # A changed canonical address invalidates its old physical-site
            # identity, including aliases whose own coordinates remain valid.
            if site_id in changed_addresses:
                site_id = None
            if identifier in points:
                point = points[identifier]
                latitude, longitude, source_label = point.latitude, point.longitude, point.source
                verified, status = point.verified, point.operational_status
                site_id = point.site_id
                if point.binding is not None and point.binding != normalized_address(
                    fields["address"], fields["city"], fields["state"]
                ):
                    raise CommandError(
                        f"Coordinate mapping for station {identifier} belongs to a different address."
                    )
            records.append(
                FuelStation(
                    opis_id=identifier,
                    **fields,
                    latitude=latitude,
                    longitude=longitude,
                    coordinate_source=source_label,
                    coordinate_verified=verified,
                    operational_status=status,
                    site_id=site_id,
                    active=True,
                )
            )
        FuelStation.objects.bulk_create(
            records,
            update_conflicts=True,
            unique_fields=["opis_id"],
            update_fields=[
                "name",
                "address",
                "city",
                "state",
                "price",
                "price_observations",
                "latitude",
                "longitude",
                "coordinate_source",
                "coordinate_verified",
                "operational_status",
                "site_id",
                "active",
            ],
        )
        # Preserve historical rows instead of deleting data on a new import.
        FuelStation.objects.exclude(opis_id__in=rows).update(active=False)
        revision = refresh_revision(counts)
    mapped = FuelStation.objects.filter(active=True, coordinate_verified=True).count()
    return {
        **counts,
        "us_stations": len(rows),
        "mapped_stations": mapped,
        "unmapped_stations": len(rows) - mapped,
        "revision": revision,
    }
