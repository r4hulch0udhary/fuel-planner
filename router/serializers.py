"""
Router app — serializers.

Validates incoming API input and serializes outgoing response data.
Uses DRF serializers exclusively so the API is Postman/OpenAPI-friendly.
"""

from rest_framework import serializers


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


class RouteRequestSerializer(serializers.Serializer):
    """Validates the POST /api/route/ request body."""

    origin = serializers.CharField(
        max_length=255,
        help_text="Starting location, e.g. 'Chicago, IL' or a full address.",
    )
    destination = serializers.CharField(
        max_length=255,
        help_text="Ending location, e.g. 'Los Angeles, CA'.",
    )
    mpg = serializers.FloatField(
        default=10.0,
        min_value=1.0,
        max_value=200.0,
        help_text="Vehicle fuel efficiency in miles per gallon (default: 10).",
    )
    tank_range_miles = serializers.FloatField(
        default=500.0,
        min_value=100.0,
        max_value=1500.0,
        help_text=(
            "Maximum driving range on a full tank in miles (default: 500). "
            "Minimum 100 miles — US highway gas stations can be 60-80 miles "
            "apart in rural areas, so smaller values will fail to plan a route."
        ),
    )


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


class FuelStopSerializer(serializers.Serializer):
    """Serializes a single recommended fuel stop."""

    station_id = serializers.IntegerField()
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    retail_price = serializers.FloatField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    gallons_to_fill = serializers.FloatField()
    cost_at_stop = serializers.FloatField()
    miles_from_previous = serializers.FloatField()


class RouteResponseSerializer(serializers.Serializer):
    """Serializes the full route optimization response."""

    origin = serializers.CharField()
    destination = serializers.CharField()
    total_distance_miles = serializers.FloatField()
    total_fuel_cost = serializers.FloatField()
    total_gallons = serializers.FloatField()
    fuel_stops = FuelStopSerializer(many=True)
    route_geometry = serializers.ListField(
        child=serializers.ListField(child=serializers.FloatField()),
        help_text="GeoJSON LineString coordinates [[lon, lat], ...].",
    )
    tank_range_miles = serializers.FloatField(
        help_text="Tank range used for this request (miles).",
    )
    mpg = serializers.FloatField(
        help_text="MPG used for this request.",
    )
    warning = serializers.CharField(
        allow_blank=True,
        help_text="Non-empty when the route could not be fully planned (e.g. tank range too small).",
    )
