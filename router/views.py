"""
Router app — views.

drf-spectacular requires DRF's APIView / @api_view so it can introspect
the schema.  DRF's dispatch loop is synchronous, so async def methods
return a coroutine instead of a Response — crashing at finalize_response.

Solution: the public DRF handler methods (get / post) are *synchronous*
but immediately delegate to private async coroutines using
``asgiref.sync.async_to_sync``.  This preserves full async I/O on ASGI
(all awaits run inside the same event loop) while keeping DRF happy.

Endpoints:
  POST /api/route/  → compute optimal fuel stops for a route
  GET  /api/health/ → health check / liveness probe

API docs:
  /api/docs/   → Swagger UI
  /api/redoc/  → ReDoc
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

from asgiref.sync import async_to_sync, sync_to_async
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
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
@api_view(["GET"])
def health_check(request: Request) -> Response:
    """
    GET /api/health/

    Simple liveness probe. Returns the count of geocoded stations
    so you can verify the import_stations command has been run.
    """
    # Synchronous ORM count — fast single scalar query.
    count = FuelStation.objects.filter(latitude__isnull=False).count()
    return Response({"status": "ok", "geocoded_stations": count})


# ---------------------------------------------------------------------------
# Route optimizer view
# ---------------------------------------------------------------------------


class RouteView(APIView):
    """
    POST /api/route/

    Accepts JSON body::

        {
            "origin": "Chicago, IL",
            "destination": "Los Angeles, CA",
            "mpg": 10,
            "tank_range_miles": 500
        }

    Execution pipeline:
      1. Validate input (DRF serializer).
      2. Geocode origin + destination concurrently (2 async HTTP calls).
      3. Fetch OSRM driving route (1 async HTTP call).
      4. Load geocoded stations from DB (async ORM).
      5. Run in-memory greedy fuel optimizer (pure NumPy, no I/O).
      6. Return JSON.
    """

    @extend_schema(
        tags=["Route"],
        summary="Compute cheapest fuel stop plan",
        description=(
            "Given an origin and destination within the USA, returns the optimal "
            "(cheapest) set of fuel stops so the vehicle never runs out of fuel. "
            "Uses a 500-mile tank range and 10 mpg by default. "
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
    def post(self, request: Request, *args, **kwargs) -> Response:
        """
        Handle POST /api/route/.

        DRF's dispatch is synchronous — we delegate immediately to the
        private async implementation via async_to_sync so that all I/O
        (geocoding, OSRM, DB) still runs fully async on the ASGI event loop.
        """
        return async_to_sync(self._async_post)(request)

    # ------------------------------------------------------------------
    # Private async implementation
    # ------------------------------------------------------------------

    async def _async_post(self, request: Request) -> Response:
        """
        Full async pipeline for route optimisation.

        Kept private so drf-spectacular only introspects the public ``post``
        method above (which carries the @extend_schema decorator).
        """

        # -- 1. Validate request body --
        serializer = RouteRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({"errors": serializer.errors}, status=400)

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
            return Response({"error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Geocoding failed: %s", exc)
            return Response(
                {"error": "Geocoding service unavailable. Try again."},
                status=503,
            )

        # -- 3. Fetch driving route from OSRM (single HTTP call) --
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
            return Response(
                {"error": "Routing service unavailable. Try again."},
                status=503,
            )

        # -- 4. Load all geocoded stations from DB --
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
        return Response(
            {
                "origin": result.origin,
                "destination": result.destination,
                "total_distance_miles": result.total_distance_miles,
                "total_fuel_cost": result.total_fuel_cost,
                "total_gallons": result.total_gallons,
                "fuel_stops": [asdict(stop) for stop in result.fuel_stops],
                "route_geometry": result.route_geometry,
                # Echo back vehicle parameters so the UI can render contextual
                # messages (e.g. "no stops needed" vs partial-route warning).
                "tank_range_miles": tank_range_miles,
                "mpg": mpg,
                "warning": result.warning,
            },
            status=200,
        )
