"""
Geocoding service for origin / destination lookups.

Uses Nominatim (OpenStreetMap) — free, no API key, 1 req/s limit.
Only called for the start/end addresses entered by the user;
station coordinates are pre-loaded at import time.
"""

from __future__ import annotations

import logging

import httpx

from .osrm import Coordinate

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


async def geocode_address(address: str) -> Coordinate:
    """
    Geocode a free-text US address string to a WGS-84 coordinate.

    Args:
        address: Human-readable address, e.g. "Chicago, IL" or
                 "1600 Pennsylvania Ave NW, Washington DC".

    Returns:
        Coordinate with the best-match lat/lon.

    Raises:
        ValueError: If Nominatim returns no results.
        httpx.HTTPError: On network / HTTP errors.
    """
    params = {
        "q": address,
        "format": "json",
        "limit": 1,
        "countrycodes": "us",
    }

    async with httpx.AsyncClient(
        headers={"User-Agent": "FuelFinderApp/1.0 (assignment)"},
        timeout=10.0,
    ) as client:
        response = await client.get(NOMINATIM_URL, params=params)
        response.raise_for_status()
        results = response.json()

    if not results:
        raise ValueError(f"Could not geocode address: '{address}'")

    best = results[0]
    coord = Coordinate(lat=float(best["lat"]), lon=float(best["lon"]))
    logger.info("Geocoded '%s' → %s", address, coord)
    return coord
