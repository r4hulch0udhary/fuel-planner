"""
OSRM routing service.

Wraps the OSRM Route API (open-source, no API key, self-hostable).
Makes a single HTTP call to get the full route geometry, total distance,
and a list of waypoints along the route at configurable intervals.

OSRM docs: http://project-osrm.org/docs/v5.24.0/api/
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Transfer Objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Coordinate:
    """A WGS-84 geographic coordinate."""

    lat: float
    lon: float

    def as_osrm(self) -> str:
        """OSRM expects 'lon,lat' order."""
        return f"{self.lon},{self.lat}"

    def as_list(self) -> list[float]:
        """Return [lon, lat] for GeoJSON compatibility."""
        return [self.lon, self.lat]


@dataclass
class RouteResult:
    """Full route result returned by OSRM."""

    # Total distance in miles
    distance_miles: float
    # Total duration in seconds
    duration_seconds: float
    # GeoJSON LineString coordinates [[lon, lat], ...]
    geometry: list[list[float]]
    # Evenly-spaced sample points along the route for station candidate lookup
    waypoints: list[Coordinate] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class OSRMService:
    """
    Async client for the OSRM Route API.

    Designed for a single call per route request: we fetch the full
    route geometry once, then derive candidate waypoints in-process
    without any additional network calls.
    """

    _METERS_PER_MILE: float = 1609.344

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or settings.OSRM_BASE_URL).rstrip("/")

    async def get_route(
        self,
        origin: Coordinate,
        destination: Coordinate,
        waypoint_interval_miles: float = 100.0,
    ) -> RouteResult:
        """
        Fetch a driving route from origin to destination.

        Makes exactly **one** request to the OSRM API.
        Waypoints along the route are sampled in-process from the
        returned geometry — no extra API calls.

        Args:
            origin: Starting coordinate.
            destination: Ending coordinate.
            waypoint_interval_miles: Distance between sampled waypoints (miles).

        Returns:
            RouteResult with geometry, distance, and evenly-spaced waypoints.

        Raises:
            RuntimeError: If OSRM returns a non-OK response.
        """
        url = self._build_url(origin, destination)
        params = {
            "overview": "full",        # full geometry, not simplified
            "geometries": "geojson",   # GeoJSON format
            "steps": "false",          # we don't need turn-by-turn steps
        }

        logger.info("Fetching route from OSRM: %s", url)

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data: dict[str, Any] = response.json()

        return self._parse_response(data, waypoint_interval_miles)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_url(self, origin: Coordinate, destination: Coordinate) -> str:
        """Build the OSRM route endpoint URL."""
        coords = f"{origin.as_osrm()};{destination.as_osrm()}"
        return f"{self._base_url}/route/v1/driving/{coords}"

    def _parse_response(
        self, data: dict[str, Any], waypoint_interval_miles: float
    ) -> RouteResult:
        """
        Parse the raw OSRM JSON response into a RouteResult.

        Samples waypoints at ``waypoint_interval_miles`` intervals along
        the route geometry so the optimizer can search for nearby stations.
        """
        if data.get("code") != "Ok":
            raise RuntimeError(
                f"OSRM error: {data.get('code')} — {data.get('message', 'unknown')}"
            )

        route = data["routes"][0]
        distance_meters: float = route["distance"]
        duration_seconds: float = route["duration"]
        coords: list[list[float]] = route["geometry"]["coordinates"]  # [lon, lat]

        distance_miles = distance_meters / self._METERS_PER_MILE

        # Sample points at regular intervals for station candidate searches
        waypoints = self._sample_waypoints(coords, waypoint_interval_miles)

        logger.info(
            "Route: %.1f miles, %d geometry points, %d waypoints sampled.",
            distance_miles,
            len(coords),
            len(waypoints),
        )

        return RouteResult(
            distance_miles=distance_miles,
            duration_seconds=duration_seconds,
            geometry=coords,
            waypoints=waypoints,
        )

    def _sample_waypoints(
        self,
        coords: list[list[float]],
        interval_miles: float,
    ) -> list[Coordinate]:
        """
        Sample coordinates from the route geometry at ``interval_miles`` intervals.

        Uses cumulative arc-length along the polyline for accurate spacing.
        The origin and destination are always included.

        Args:
            coords: GeoJSON coordinates [[lon, lat], ...].
            interval_miles: Desired spacing between sampled points.

        Returns:
            List of Coordinate objects evenly distributed along the route.
        """
        if not coords:
            return []

        import math

        def haversine_miles(a: list[float], b: list[float]) -> float:
            """Great-circle distance in miles between two [lon, lat] points."""
            lon1, lat1 = math.radians(a[0]), math.radians(a[1])
            lon2, lat2 = math.radians(b[0]), math.radians(b[1])
            dlat = lat2 - lat1
            dlon = lon2 - lon1
            h = (
                math.sin(dlat / 2) ** 2
                + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
            )
            return 3958.8 * 2 * math.asin(math.sqrt(h))

        waypoints: list[Coordinate] = [Coordinate(lat=coords[0][1], lon=coords[0][0])]
        accumulated = 0.0
        next_sample = interval_miles

        for i in range(1, len(coords)):
            segment = haversine_miles(coords[i - 1], coords[i])
            accumulated += segment
            if accumulated >= next_sample:
                waypoints.append(Coordinate(lat=coords[i][1], lon=coords[i][0]))
                next_sample += interval_miles

        # Always include the destination
        waypoints.append(Coordinate(lat=coords[-1][1], lon=coords[-1][0]))
        return waypoints
