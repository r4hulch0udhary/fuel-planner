"""
Router app — URL configuration.

All routes are prefixed with /api/ from the root urls.py.
"""

from django.urls import path

from .views import RouteView, health_check

urlpatterns = [
    # POST /api/route/  → fuel stop optimization
    path("route/", RouteView.as_view(), name="route"),
    # GET  /api/health/ → liveness probe
    path("health/", health_check, name="health"),
]
