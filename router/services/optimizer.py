"""
Fuel stop optimizer.

Given a route (as a list of waypoints from OSRM), finds the cheapest
set of fuel stops such that the vehicle never runs out of fuel.

Algorithm (greedy look-ahead):
  - The vehicle starts with a full tank (500-mile range, 10 mpg).
  - At each position, we look ahead up to the remaining tank range.
  - Among all stations reachable from the current position, we pick
    the cheapest one that is still reachable before we run dry.
  - We stop fuelling when we can reach the destination directly.

All heavy computation (distance matrix, candidate filtering) is done
with NumPy vectorized operations so it runs fast in the async event
loop without blocking (pure Python math, no I/O).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np
from django.conf import settings

from .osrm import Coordinate, RouteResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Transfer Objects
# ---------------------------------------------------------------------------


@dataclass
class FuelStop:
    """A single recommended fuel stop."""

    station_id: int
    name: str
    address: str
    city: str
    state: str
    retail_price: float
    latitude: float
    longitude: float
    gallons_to_fill: float
    cost_at_stop: float
    miles_from_previous: float


@dataclass
class OptimizationResult:
    """Full result returned to the API view."""

    origin: str
    destination: str
    total_distance_miles: float
    total_fuel_cost: float
    total_gallons: float
    fuel_stops: list[FuelStop] = field(default_factory=list)
    route_geometry: list[list[float]] = field(default_factory=list)
    warning: str = ""


# ---------------------------------------------------------------------------
# Optimizer
# ---------------------------------------------------------------------------


METERS_PER_MILE = 1609.344
SEARCH_RADIUS_DEG = 0.5   # ~35 miles bounding-box pre-filter before exact Haversine


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two WGS-84 points."""
    r = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


class FuelOptimizer:
    """
    Greedy look-ahead optimizer for cheapest fuel stops along a route.

    Accepts pre-loaded station data as NumPy arrays so it can run entirely
    in memory without any database queries during the hot path.
    """

    def __init__(
        self,
        mpg: float = settings.VEHICLE_MPG,
        tank_range_miles: float = settings.VEHICLE_TANK_RANGE_MILES,
    ) -> None:
        """
        Args:
            mpg: Vehicle fuel efficiency in miles per gallon.
            tank_range_miles: Maximum range on a full tank (miles).
        """
        self._mpg = mpg
        self._tank_range_miles = tank_range_miles
        self._tank_capacity_gallons = tank_range_miles / mpg

    def optimize(
        self,
        route: RouteResult,
        stations: list[dict],  # already fetched from DB by the view
        origin_label: str,
        destination_label: str,
    ) -> OptimizationResult:
        """
        Compute the cheapest fuel stop plan for the given route.

        Args:
            route: RouteResult from OSRMService.
            stations: List of station dicts with keys:
                      id, name, address, city, state, retail_price, latitude, longitude.
            origin_label: Human-readable origin string (for the response).
            destination_label: Human-readable destination string (for the response).

        Returns:
            OptimizationResult with fuel stops and total cost.
        """
        if not stations:
            logger.warning("No geocoded stations available — returning empty plan.")
            return OptimizationResult(
                origin=origin_label,
                destination=destination_label,
                total_distance_miles=route.distance_miles,
                total_fuel_cost=0.0,
                total_gallons=0.0,
                route_geometry=route.geometry,
            )

        # Build NumPy arrays for vectorized distance calculations
        st_lats = np.array([s["latitude"] for s in stations], dtype=np.float64)
        st_lons = np.array([s["longitude"] for s in stations], dtype=np.float64)
        st_prices = np.array([s["retail_price"] for s in stations], dtype=np.float64)

        destination_coord = route.waypoints[-1]
        waypoints = route.waypoints

        # Pre-filter stations to those within MAX_DETOUR_MILES of the actual
        # route geometry (not just sparse waypoints).  We downsample the dense
        # polyline to ~1-mile intervals internally for speed, then run a
        # fully vectorised haversine distance matrix (N stations × M points).
        stations = self._filter_near_route_geometry(
            stations, st_lats, st_lons, route.geometry, radius_miles=1.5
        )
        if not stations:
            logger.warning(
                "No stations found within 1.5 miles of the route — "
                "route may pass through areas without fuel stations in the dataset."
            )
            return OptimizationResult(
                origin=origin_label,
                destination=destination_label,
                total_distance_miles=route.distance_miles,
                total_fuel_cost=0.0,
                total_gallons=0.0,
                route_geometry=route.geometry,
            )

        # Rebuild NumPy arrays for the filtered subset
        st_lats = np.array([s["latitude"] for s in stations], dtype=np.float64)
        st_lons = np.array([s["longitude"] for s in stations], dtype=np.float64)
        st_prices = np.array([s["retail_price"] for s in stations], dtype=np.float64)

        fuel_stops: list[FuelStop] = []
        current_pos = waypoints[0]
        remaining_range = self._tank_range_miles  # start with a full tank

        visited_station_ids: set[int] = set()

        while True:
            # Distance to destination from current position
            dist_to_dest = _haversine_miles(
                current_pos.lat, current_pos.lon,
                destination_coord.lat, destination_coord.lon,
            )

            # If we can reach the destination, we're done.
            if dist_to_dest <= remaining_range:
                break

            # Find the best (cheapest) station we can reach from here
            # that still allows us to continue toward the destination.
            best_stop = self._find_best_stop(
                current_pos=current_pos,
                destination=destination_coord,
                remaining_range=remaining_range,
                stations=stations,
                st_lats=st_lats,
                st_lons=st_lons,
                st_prices=st_prices,
                visited=visited_station_ids,
            )

            if best_stop is None:
                logger.error(
                    "No reachable station found from (%.4f, %.4f) with %.1f mi range — "
                    "tank range may be too small or station data is sparse here.",
                    current_pos.lat,
                    current_pos.lon,
                    remaining_range,
                )
                # Return what we have so far with a warning; don't silently
                # return an optimistic total-cost figure computed from origin.
                stops_cost = sum(s.cost_at_stop for s in fuel_stops)
                stops_gallons = sum(s.gallons_to_fill for s in fuel_stops)
                return OptimizationResult(
                    origin=origin_label,
                    destination=destination_label,
                    total_distance_miles=round(route.distance_miles, 3),
                    total_fuel_cost=round(stops_cost, 2),
                    total_gallons=round(stops_gallons, 3),
                    fuel_stops=fuel_stops,
                    route_geometry=route.geometry,
                    warning=(
                        f"Could not find a reachable fuel station within "
                        f"{self._tank_range_miles:.0f} miles of your current position. "
                        f"The tank range may be too small to bridge the gap between "
                        f"stations on this route — try a larger tank range."
                    ),
                )

            # How far did we travel to reach this stop?
            miles_driven = _haversine_miles(
                current_pos.lat, current_pos.lon,
                best_stop["latitude"], best_stop["longitude"],
            )
            remaining_range -= miles_driven

            # Fill up to a full tank
            gallons_needed = (self._tank_range_miles - remaining_range) / self._mpg

            # Skip stops that would add < 1 gallon — the algorithm chose a
            # station so close to the last one that the tank is still nearly full.
            # Move to the station (update position) but don't record a stop.
            current_pos = Coordinate(
                lat=best_stop["latitude"],
                lon=best_stop["longitude"],
            )
            visited_station_ids.add(best_stop["id"])

            if gallons_needed < 1.0:
                remaining_range = self._tank_range_miles  # fill up anyway, free move
                continue

            cost = gallons_needed * best_stop["retail_price"]

            fuel_stops.append(
                FuelStop(
                    station_id=best_stop["id"],
                    name=best_stop["name"],
                    address=best_stop["address"],
                    city=best_stop["city"],
                    state=best_stop["state"],
                    retail_price=best_stop["retail_price"],
                    latitude=best_stop["latitude"],
                    longitude=best_stop["longitude"],
                    gallons_to_fill=round(gallons_needed, 3),
                    cost_at_stop=round(cost, 2),
                    miles_from_previous=round(miles_driven, 1),
                )
            )

            remaining_range = self._tank_range_miles  # full tank after stop

        # --- Compute total fuel cost (including the final leg) ---
        # Each stop's cost covers the miles consumed *getting to* that stop.
        # We still need to account for the last leg (last stop → destination).
        stops_cost: float = sum(s.cost_at_stop for s in fuel_stops)
        stops_gallons: float = sum(s.gallons_to_fill for s in fuel_stops)

        # Final leg: from last stop (or origin if no stops) to destination
        if fuel_stops:
            last_lat = fuel_stops[-1].latitude
            last_lon = fuel_stops[-1].longitude
            last_price = fuel_stops[-1].retail_price
        else:
            last_lat = waypoints[0].lat
            last_lon = waypoints[0].lon
            last_price = float(st_prices.min()) if len(st_prices) else 0.0

        last_leg_miles = _haversine_miles(
            last_lat, last_lon,
            destination_coord.lat, destination_coord.lon,
        )
        last_leg_gallons = last_leg_miles / self._mpg
        last_leg_cost = last_leg_gallons * last_price

        total_gallons = stops_gallons + last_leg_gallons
        total_cost = stops_cost + last_leg_cost

        logger.info(
            "Optimization complete: %d stops, $%.2f total, %.1f gal.",
            len(fuel_stops),
            total_cost,
            total_gallons,
        )

        return OptimizationResult(
            origin=origin_label,
            destination=destination_label,
            total_distance_miles=round(route.distance_miles, 3),
            total_fuel_cost=round(total_cost, 2),
            total_gallons=round(total_gallons, 3),
            fuel_stops=fuel_stops,
            route_geometry=route.geometry,
        )

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _filter_near_route_geometry(
        self,
        stations: list[dict],
        st_lats: np.ndarray,
        st_lons: np.ndarray,
        geometry: list[list[float]],
        radius_miles: float = 1.5,
    ) -> list[dict]:
        """
        Return only stations within ``radius_miles`` of the actual route line.

        Uses the **dense** OSRM geometry (potentially thousands of [lon, lat]
        points) rather than the sparse sampled waypoints, so the corridor
        tightly hugs the real road without inflating detour allowances.

        For performance the geometry is first downsampled to one reference
        point every ~1 mile (using cumulative arc length).  Then a fully
        vectorised NumPy haversine distance matrix
        ``(N stations) × (M reference points)`` is computed; each station
        is kept when its minimum distance to any reference point is
        ≤ ``radius_miles``.

        Args:
            stations:     Full list of station dicts from the DB.
            st_lats:      Pre-built latitude array (same order as stations).
            st_lons:      Pre-built longitude array.
            geometry:     GeoJSON coordinates [[lon, lat], …] from OSRM.
            radius_miles: Maximum straight-line detour from the route (miles).

        Returns:
            Filtered list — only stations that are genuinely near the route.
        """
        if not geometry or not stations:
            return stations

        # ── Step 1: downsample geometry to ~1-mile reference points ───────────
        # This keeps the distance matrix manageable even for long routes
        # while preserving enough spatial resolution for a 1.5-mile corridor.
        ref_lons: list[float] = [geometry[0][0]]
        ref_lats: list[float] = [geometry[0][1]]
        accumulated = 0.0
        SAMPLE_INTERVAL_MILES = 1.0

        for i in range(1, len(geometry)):
            prev, curr = geometry[i - 1], geometry[i]
            # Fast equirectangular approximation for short segments (< 5 miles)
            dlat = math.radians(curr[1] - prev[1])
            dlon = math.radians(curr[0] - prev[0])
            mid_lat = math.radians((curr[1] + prev[1]) / 2)
            segment = 3958.8 * math.sqrt(dlat ** 2 + (math.cos(mid_lat) * dlon) ** 2)
            accumulated += segment
            if accumulated >= SAMPLE_INTERVAL_MILES:
                ref_lons.append(curr[0])
                ref_lats.append(curr[1])
                accumulated = 0.0

        # Always include the last point
        ref_lons.append(geometry[-1][0])
        ref_lats.append(geometry[-1][1])

        logger.debug(
            "Route geometry: %d raw points → %d reference points (1-mile interval)",
            len(geometry),
            len(ref_lats),
        )

        # ── Step 2: vectorised haversine distance matrix ──────────────────────
        rp_lats = np.radians(np.array(ref_lats, dtype=np.float64))  # (M,)
        rp_lons = np.radians(np.array(ref_lons, dtype=np.float64))  # (M,)
        st_lats_rad = np.radians(st_lats)                            # (N,)
        st_lons_rad = np.radians(st_lons)                            # (N,)

        # Broadcasting shapes: (N, 1) and (1, M) → (N, M)
        dlat = st_lats_rad[:, None] - rp_lats[None, :]   # (N, M)
        dlon = st_lons_rad[:, None] - rp_lons[None, :]   # (N, M)
        cos_st = np.cos(st_lats_rad)[:, None]             # (N, 1)
        cos_rp = np.cos(rp_lats)[None, :]                # (1, M)

        a = np.sin(dlat / 2) ** 2 + cos_st * cos_rp * np.sin(dlon / 2) ** 2
        # Minimum distance from each station to any reference point
        min_dist_miles = (
            3958.8 * 2 * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0).min(axis=1)))
        )  # shape (N,)

        near_mask = min_dist_miles <= radius_miles
        nearby = [s for s, keep in zip(stations, near_mask) if keep]

        logger.info(
            "Route proximity filter: %d/%d stations within %.1f mile(s) of route.",
            len(nearby),
            len(stations),
            radius_miles,
        )
        return nearby

    def _find_best_stop(
        self,
        current_pos: Coordinate,
        destination: Coordinate,
        remaining_range: float,
        stations: list[dict],
        st_lats: np.ndarray,
        st_lons: np.ndarray,
        st_prices: np.ndarray,
        visited: set[int],
    ) -> dict | None:
        """
        Find the cheapest reachable station that keeps us progressing.

        Strategy:
          1. Bounding-box pre-filter (fast, no trig).
          2. Exact Haversine distance filter — must be ≤ remaining range.
          3. Must be closer to destination than current position (progress check).
          4. Among candidates, pick the cheapest.

        Returns the station dict or None if no candidate is found.
        """
        clat, clon = current_pos.lat, current_pos.lon
        dlat, dlon = destination.lat, destination.lon

        # Current straight-line distance to destination
        current_dist_to_dest = _haversine_miles(clat, clon, dlat, dlon)

        # 1) Rough bounding-box pre-filter using NumPy (no trig, very fast)
        lat_delta = remaining_range / 69.0
        lon_delta = remaining_range / (69.0 * abs(math.cos(math.radians(clat))) + 1e-9)

        mask = (
            (st_lats >= clat - lat_delta) & (st_lats <= clat + lat_delta) &
            (st_lons >= clon - lon_delta) & (st_lons <= clon + lon_delta)
        )
        candidate_indices = np.where(mask)[0]

        if candidate_indices.size == 0:
            return None

        best: dict | None = None
        best_price = float("inf")

        for idx in candidate_indices:
            s = stations[idx]
            if s["id"] in visited:
                continue

            dist = _haversine_miles(clat, clon, s["latitude"], s["longitude"])
            if dist > remaining_range:
                # Not reachable on current tank
                continue

            # Soft progress check: the station must be no more than 50 miles
            # *further* from the destination than our current position.
            # This allows minor detours for a cheap station while preventing
            # large backtracking loops.
            stop_dist_to_dest = _haversine_miles(
                s["latitude"], s["longitude"], dlat, dlon
            )
            if stop_dist_to_dest > current_dist_to_dest + 50:
                continue

            price = s["retail_price"]
            if price < best_price:
                best_price = price
                best = s

        return best
