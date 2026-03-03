"""Stations app configuration."""

from django.apps import AppConfig


class StationsConfig(AppConfig):
    """Django app config for the stations app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "stations"
    verbose_name = "Fuel Stations"
