"""
Stations app — models.

FuelStation stores every truck stop loaded from the CSV dataset.
Coordinates are pre-geocoded at import time so the routing algorithm
never has to make geocoding API calls at request time.
"""

from django.db import models


class FuelStation(models.Model):
    """
    A single fuel station from the OPIS dataset.

    ``latitude`` / ``longitude`` are populated by the
    ``import_stations`` management command which geocodes
    each unique city/state pair in bulk.
    """

    opis_id: int = models.IntegerField(
        db_index=True,
        help_text="OPIS Truckstop ID.",
    )
    name: str = models.CharField(max_length=255, help_text="Truckstop display name.")
    address: str = models.CharField(max_length=255)
    city: str = models.CharField(max_length=100, db_index=True)
    state: str = models.CharField(max_length=2, db_index=True)
    rack_id: int = models.IntegerField()
    retail_price: float = models.FloatField(
        help_text="Retail price in USD per gallon."
    )

    # Geocoded at import time — null until the command has run.
    latitude: float = models.FloatField(null=True, blank=True)
    longitude: float = models.FloatField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fuel_stations"
        ordering = ["state", "city", "retail_price"]
        verbose_name = "Fuel Station"
        verbose_name_plural = "Fuel Stations"
        indexes = [
            models.Index(fields=["latitude", "longitude"]),
            models.Index(fields=["retail_price"]),
        ]

    def __str__(self) -> str:
        return (
            f"{self.name} — {self.city}, {self.state}"
            f" (${self.retail_price:.3f}/gal)"
        )

    @property
    def has_coordinates(self) -> bool:
        """Return True if this station has been geocoded."""
        return self.latitude is not None and self.longitude is not None
