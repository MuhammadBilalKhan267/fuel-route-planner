from typing import ClassVar

from django.db import models


class FuelStation(models.Model):
    class OperationalStatus(models.TextChoices):
        UNKNOWN = "unknown"
        OPEN = "open"
        CLOSED = "closed"

    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=120)
    state = models.CharField(max_length=2)
    price = models.DecimalField(max_digits=14, decimal_places=8)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    coordinate_source = models.CharField(max_length=120, blank=True)
    coordinate_verified = models.BooleanField(default=False)
    operational_status = models.CharField(
        max_length=7, choices=OperationalStatus.choices, default=OperationalStatus.UNKNOWN
    )
    site_id = models.PositiveIntegerField(null=True, blank=True)
    price_observations = models.PositiveIntegerField(default=1)
    active = models.BooleanField(default=True)

    class Meta:
        constraints: ClassVar[list] = [
            models.CheckConstraint(condition=models.Q(price__gt=0), name="positive_station_price"),
            models.CheckConstraint(
                condition=(
                    models.Q(latitude__isnull=True, longitude__isnull=True)
                    | models.Q(latitude__isnull=False, longitude__isnull=False)
                ),
                name="coordinate_pair_present_or_absent",
            ),
            models.CheckConstraint(
                condition=models.Q(coordinate_verified=False)
                | models.Q(latitude__isnull=False, longitude__isnull=False),
                name="verified_coordinate_pair_present",
            ),
            models.CheckConstraint(
                condition=models.Q(operational_status__in=["unknown", "open", "closed"]),
                name="valid_station_operational_status",
            ),
        ]


class StationDataset(models.Model):
    revision = models.CharField(max_length=64)
    source_rows = models.PositiveIntegerField(default=0)
    non_us_rows = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)
