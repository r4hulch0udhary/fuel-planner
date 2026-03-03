"""
Router app — async views.

All views are fully async using Django's native async view support.
DB access uses sync_to_async so we never block the event loop.

Endpoints:
  POST /api/route/  → compute optimal fuel stops for a route
  GET  /api/health/ → health check / liveness probe

API docs:
  /api/docs/   → Swagger UI
  /api/redoc/  → ReDoc
"""

from __future__ import annotations

import json
import asyncio
import logging
from dataclasses import asdict

from asgiref.sync import sync_to_async
from django.http import JsonResponse
from django.views import View
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiExample

from stations.models import FuelStation
from .serializers import RouteRequestSerializer, RouteResponseSerializer
from .services.geocoding import geocode_address
from .services.optimizer import FuelOptimizer
from .services.osrm import OSRMService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@extend_schema(
    tags=["Health"],
    summary="Liveness probe",
    description=(
        "Returns HTTP 200 with the count of geocoded stations in the database. "
        "If `geocoded_stations` is 0, run `python manage.py import_stations` first."
    ),
    responses={
        200: OpenApiResponse(
            description="Service is healthy.",
            examples=[
                OpenApiExample(
                    "Healthy",
                    value={"status": "ok", "geocoded_stations": 8151},
                )
            ],
        )
    },
)
async def health_check(request):  # noqa: ANN001
    """
    GET /api/health/

    Simple liveness probe. Returns the count of geocoded stations
    so you can verify the import_stations command has been run.
    """
    count = await sync_to_async(
        FuelStation.objects.filter(latitude__isnull=False).count
    )()
    return JsonResponse({"status": "ok", "geocoded_stations": count}, status=200)


# ---------------------------------------------------------------------------
# Route optimizer view
# ---------------------------------------------------------------------------


@method_decorator(csrf_exempt, name="dispatch")
class RouteView(View):
    """
    POST /api/route/

    Accepts JSON body::

        {
            "origin": "Chicago, IL",
            "destination": "Los Angeles, CA",
            "mpg": 10,
            "tank_range_miles": 500
        }

    Execution pipeline (fully async, ~1–2 external API calls total):
      1. Validate input (sync serializer, no I/O).
      2. Geocode origin + destination concurrently (2 async calls).
      3. Fetch OSRM route   (1 async HTTP call).
      4. Load geocoded stations from DB (async ORM).
      5. Run in-memory optimizer (pure Python/NumPy, no I/O).
      6. Return JSON.
    """

    @extend_schema(
        tags=["Route"],
        summary="Compute cheapest fuel stop plan",
        description=(
            "Given an origin and destination within the USA, returns the optimal "
            "(cheapest) set of fuel stops so the vehicle never runs out of fuel "
            "(max 500-mile tank range, 10 mpg by default). "
            "Makes exactly **one** call to the OSRM routing API. "
            "Fuel stop selection uses a greedy look-ahead algorithm over "
            "pre-geocoded OPIS station data."
        ),
        request=RouteRequestSerializer,
        responses={
            200: RouteResponseSerializer,
            400: OpenApiResponse(description="Invalid input — missing fields or bad geocode."),
            503: OpenApiResponse(description="OSRM or geocoding service unavailable."),
        },
        examples=[
            OpenApiExample(
                "Chicago to LA",
                request_only=True,
                value={
                    "origin": "Chicago, IL",
                    "destination": "Los Angeles, CA",
                    "mpg": 10,
                    "tank_range_miles": 500,
                },
            ),
            OpenApiExample(
                "New York to San Francisco",
                request_only=True,
                value={
                    "origin": "New York, NY",
                    "destination": "San Francisco, CA",
                    "mpg": 10,
                    "tank_range_miles": 500,
                },
            ),
        ],
    )
    async def post(self, request, *args, **kwargs):  # noqa: ANN001
        """Handle POST request for route optimization."""

        # -- 1. Parse & validate request body --
        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON body."}, status=400)

        serializer = RouteRequestSerializer(data=body)
        if not serializer.is_valid():
            return JsonResponse({"errors": serializer.errors}, status=400)

        data = serializer.validated_data
        origin_str: str = data["origin"]
        destination_str: str = data["destination"]
        mpg: float = data["mpg"]
        tank_range_miles: float = data["tank_range_miles"]

        # -- 2. Geocode origin and destination concurrently --
        try:
            origin_coord, dest_coord = await asyncio.gather(
                geocode_address(origin_str),
                geocode_address(destination_str),
            )
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Geocoding failed: %s", exc)
            return JsonResponse(
                {"error": "Geocoding service unavailable. Try again."},
                status=503,
            )

        # -- 3. Fetch route from OSRM (single HTTP call) --
        osrm = OSRMService()
        try:
            route = await osrm.get_route(
                origin=origin_coord,
                destination=dest_coord,
                # Sample a waypoint every 20% of tank range for good coverage
                waypoint_interval_miles=tank_range_miles * 0.2,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("OSRM routing failed: %s", exc)
            return JsonResponse(
                {"error": "Routing service unavailable. Try again."},
                status=503,
            )

        # -- 4. Load all geocoded stations from DB (async) --
        stations_qs = await sync_to_async(
            lambda: list(
                FuelStation.objects.filter(
                    latitude__isnull=False,
                    longitude__isnull=False,
                ).values(
                    "id",
                    "name",
                    "address",
                    "city",
                    "state",
                    "retail_price",
                    "latitude",
                    "longitude",
                )
            )
        )()

        # -- 5. Run the in-memory fuel optimizer (pure NumPy, no I/O) --
        optimizer = FuelOptimizer(mpg=mpg, tank_range_miles=tank_range_miles)
        result = optimizer.optimize(
            route=route,
            stations=stations_qs,
            origin_label=origin_str,
            destination_label=destination_str,
        )

        # -- 6. Serialize and return --
        return JsonResponse(
            {
                "origin": result.origin,
                "destination": result.destination,
                "total_distance_miles": result.total_distance_miles,
                "total_fuel_cost": result.total_fuel_cost,
                "total_gallons": result.total_gallons,
                "fuel_stops": [asdict(stop) for stop in result.fuel_stops],
                "route_geometry": result.route_geometry,
                # Echo back the vehicle parameters so the client can render
                # contextual messages (e.g. "no stops needed") correctly.
                "tank_range_miles": tank_range_miles,
                "mpg": mpg,
                "warning": result.warning,
            },
            status=200,
        )
