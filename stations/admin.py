"""
Stations admin registration.

Provides a searchable, filterable admin view over FuelStation records.
"""

from django.contrib import admin

from .models import FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    """Admin panel configuration for FuelStation."""

    list_display = (
        "name",
        "city",
        "state",
        "retail_price",
        "latitude",
        "longitude",
        "has_coordinates",
    )
    list_filter = ("state",)
    search_fields = ("name", "city", "state", "address")
    ordering = ("state", "city", "retail_price")
    readonly_fields = ("created_at", "updated_at")

    @admin.display(boolean=True, description="Geocoded?")
    def has_coordinates(self, obj: FuelStation) -> bool:
        """Shows a green tick if the station has lat/lon."""
        return obj.has_coordinates
