"""Router app configuration."""

from django.apps import AppConfig


class RouterConfig(AppConfig):
    """Django app config for the router app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "router"
    verbose_name = "Route Optimizer"
