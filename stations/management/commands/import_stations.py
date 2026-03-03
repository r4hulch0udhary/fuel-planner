"""
Management command: import_stations

Loads fuel station data from the CSV file into the database and
geocodesach unique (city, state) pair using the ArcGIS online geocoder
(free, no API key) with asyncio concurrency for speed.

Geocoding results are cached to ``geocode_cache.json`` so subsequent
runs are instant (only new locations are geocoded).

Usage:
    python manage.py import_stations
    python manage.py import_stations --csv path/to/other.csv
    python manage.py import_stations --skip-geocode   # skip geocoding
    python manage.py import_stations --concurrency 20  # default: 10
"""

import asyncio
import csv
import json
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from geopy.geocoders import ArcGIS

from stations.models import FuelStation


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_CSV = Path(settings.BASE_DIR) / "fuel-prices-for-be-assessment.csv"
GEOCODE_CACHE_FILE = Path(settings.BASE_DIR) / "geocode_cache.json"

# ArcGIS World Geocoding Service — free, no API key, supports concurrency.
DEFAULT_CONCURRENCY = 10   # simultaneous geocode threads
GEOCODE_TIMEOUT = 15       # seconds per request


class Command(BaseCommand):
    """
    Import fuel stations from CSV and geocode their coordinates.

    Steps:
      1. Read CSV — deduplicate by (city, state) to minimise geocoding calls.
      2. Load existing geocode cache (JSON) to skip already-known locations.
      3. Geocode new locations concurrently via ArcGIS (no key, fast).
      4. Save updated cache to disk.
      5. Bulk-create FuelStation rows (replace existing).
    """

    help = "Import fuel stations from CSV and geocode coordinates."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument(
            "--csv",
            type=Path,
            default=DEFAULT_CSV,
            help="Path to the fuel-prices CSV file.",
        )
        parser.add_argument(
            "--skip-geocode",
            action="store_true",
            default=False,
            help="Import station data without geocoding (lat/lon will be null).",
        )
        parser.add_argument(
            "--concurrency",
            type=int,
            default=DEFAULT_CONCURRENCY,
            help=f"Number of concurrent geocoding threads (default: {DEFAULT_CONCURRENCY}).",
        )
        parser.add_argument(
            "--clear-cache",
            action="store_true",
            default=False,
            help="Delete the geocode cache and re-geocode everything from scratch.",
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        csv_path: Path = options["csv"]
        skip_geocode: bool = options["skip_geocode"]
        concurrency: int = options["concurrency"]
        clear_cache: bool = options["clear_cache"]

        if not csv_path.exists():
            raise CommandError(f"CSV file not found: {csv_path}")

        self.stdout.write(self.style.MIGRATE_HEADING(f"Parsing CSV: {csv_path}"))
        rows = self._parse_csv(csv_path)
        self.stdout.write(f"  → {len(rows)} station records read.")

        # Geocode unique (city, state) pairs
        coords: dict[tuple[str, str], tuple[Optional[float], Optional[float]]] = {}
        if not skip_geocode:
            if clear_cache and GEOCODE_CACHE_FILE.exists():
                GEOCODE_CACHE_FILE.unlink()
                self.stdout.write("  → Geocode cache cleared.")

            # Load existing cache
            cache = self._load_cache()
            unique_locations = {(r["city"], r["state"]) for r in rows}
            new_locations = {
                loc for loc in unique_locations
                if f"{loc[0]}|{loc[1]}" not in cache
            }

            self.stdout.write(
                f"  → {len(unique_locations)} unique locations, "
                f"{len(new_locations)} not in cache — geocoding now…"
            )

            if new_locations:
                new_coords = asyncio.run(
                    self._geocode_all(new_locations, concurrency)
                )
                # Merge new results into cache
                for (city, state), (lat, lon) in new_coords.items():
                    cache[f"{city}|{state}"] = [lat, lon]
                self._save_cache(cache)

            # Build the full coords dict from cache
            for city, state in unique_locations:
                cached = cache.get(f"{city}|{state}", [None, None])
                coords[(city, state)] = (
                    cached[0],
                    cached[1],
                )

            geocoded = sum(1 for v in coords.values() if v[0] is not None)
            self.stdout.write(
                f"  → {geocoded}/{len(unique_locations)} locations geocoded."
            )
        else:
            self.stdout.write("  → Skipping geocoding (--skip-geocode).")

        # Persist to DB
        self.stdout.write("  → Saving to database …")
        created = self._bulk_save(rows, coords)
        self.stdout.write(
            self.style.SUCCESS(f"Done! {created} stations saved to database.")
        )

    # ------------------------------------------------------------------
    # CSV parsing
    # ------------------------------------------------------------------

    def _parse_csv(self, path: Path) -> list[dict]:
        """
        Parse the OPIS CSV file into a list of dicts.

        Handles the quoted address field that contains commas.
        """
        stations = []
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                try:
                    stations.append({
                        "opis_id": int(row["OPIS Truckstop ID"]),
                        "name": row["Truckstop Name"].strip(),
                        "address": row["Address"].strip(),
                        "city": row["City"].strip(),
                        "state": row["State"].strip(),
                        "rack_id": int(row["Rack ID"]),
                        "retail_price": float(row["Retail Price"]),
                    })
                except (ValueError, KeyError) as exc:
                    self.stderr.write(f"Skipping malformed row: {exc}")
        return stations

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _load_cache(
        self,
    ) -> dict[str, list[Optional[float]]]:
        """Load the geocode cache from disk, or return an empty dict."""
        if GEOCODE_CACHE_FILE.exists():
            try:
                return json.loads(GEOCODE_CACHE_FILE.read_text())
            except json.JSONDecodeError:
                self.stderr.write("  ⚠ Cache file corrupted — starting fresh.")
        return {}

    def _save_cache(
        self,
        cache: dict[str, list[Optional[float]]],
    ) -> None:
        """Persist the geocode cache to disk as JSON."""
        GEOCODE_CACHE_FILE.write_text(json.dumps(cache, indent=2))

    # ------------------------------------------------------------------
    # Async geocoding (ArcGIS — free, no key, concurrent-safe)
    # ------------------------------------------------------------------

    async def _geocode_all(
        self,
        locations: set[tuple[str, str]],
        concurrency: int,
    ) -> dict[tuple[str, str], tuple[Optional[float], Optional[float]]]:
        """
        Geocode ``locations`` concurrently using the ArcGIS World Geocoder.

        ArcGIS's online geocoder is free for development use with no
        mandatory API key and supports concurrent requests, making it
        much faster than Nominatim (which requires sequential, 1 req/s).

        A ``asyncio.Semaphore`` limits parallelism to avoid overwhelming
        the service.

        Returns a dict mapping (city, state) → (lat, lon) or (None, None).
        """
        geocoder = ArcGIS(timeout=GEOCODE_TIMEOUT)
        semaphore = asyncio.Semaphore(concurrency)
        completed = 0
        total = len(locations)

        async def _geocode_one(
            city: str, state: str
        ) -> tuple[tuple[str, str], tuple[Optional[float], Optional[float]]]:
            """
            Geocode a single (city, state) pair inside the semaphore.

            Uses ``asyncio.to_thread`` to run the synchronous geopy call
            in a thread-pool worker without blocking the event loop.
            """
            nonlocal completed
            async with semaphore:
                query = f"{city}, {state}, USA"
                try:
                    location = await asyncio.to_thread(
                        geocoder.geocode, query
                    )
                    if location:
                        result = (location.latitude, location.longitude)
                    else:
                        result = (None, None)
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(
                        f"  ✗ Geocode failed for {city}, {state}: {exc}"
                    )
                    result = (None, None)

            completed += 1
            if completed % 50 == 0 or completed == total:
                self.stdout.write(
                    f"    geocoded {completed}/{total} locations…"
                )
            return (city, state), result

        tasks = [_geocode_one(city, state) for city, state in locations]
        raw = await asyncio.gather(*tasks)
        return dict(raw)

    # ------------------------------------------------------------------
    # Database persistence
    # ------------------------------------------------------------------

    def _bulk_save(
        self,
        rows: list[dict],
        coords: dict[tuple[str, str], tuple[Optional[float], Optional[float]]],
    ) -> int:
        """
        Bulk-insert FuelStation objects, replacing any existing data.

        Uses bulk_create with a batch size of 500 for efficiency.
        """
        FuelStation.objects.all().delete()

        objects = []
        for row in rows:
            lat, lon = coords.get((row["city"], row["state"]), (None, None))
            objects.append(
                FuelStation(
                    opis_id=row["opis_id"],
                    name=row["name"],
                    address=row["address"],
                    city=row["city"],
                    state=row["state"],
                    rack_id=row["rack_id"],
                    retail_price=row["retail_price"],
                    latitude=lat,
                    longitude=lon,
                )
            )

        FuelStation.objects.bulk_create(objects, batch_size=500)
        return len(objects)
