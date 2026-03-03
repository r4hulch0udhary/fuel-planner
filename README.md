# Fuel Route Planner

A Django REST API that computes the **cheapest fuel stop plan** for a road trip across the USA.

Given an origin and destination, it returns:
- An optimised list of fuel stops (lowest price, within 500-mile tank range)
- Total fuel cost and gallons consumed
- A full route geometry (GeoJSON) for map rendering

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Django 5.1 + DRF | Required by the spec |
| Async server | Uvicorn (ASGI) | Full async support for Django async views |
| Routing API | OSRM (free, open-source) | No API key, self-hostable, single call |
| Geocoding | Nominatim (OSM) | Free, no API key |
| Fuel data | OPIS CSV (~8 k stations) | Pre-loaded once via management command |
| HTTP client | httpx | Native async, replaces requests |
| Frontend | Django template + Leaflet.js + Vanilla JS | No build step, no React needed; fetch() handles API calls |
| DB | SQLite (dev) / PostgreSQL (Docker) | |

---

## Local development setup

### 1. Clone and create virtualenv

```bash
git clone <repo-url>
cd assignment3

python3 -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env if you want to change OSRM_BASE_URL or use PostgreSQL
```

### 3. Run migrations

```bash
python manage.py migrate
```

### 4. Import fuel stations (geocodes ~1 800 unique city/state pairs)

> This takes ~35 minutes due to Nominatim's 1 req/s rate limit.
> Run once — results are stored in the database permanently.

```bash
python manage.py import_stations
```

To import without geocoding (fast, but /api/route/ needs stations in the DB):

```bash
python manage.py import_stations --skip-geocode
```

Custom CSV path:

```bash
python manage.py import_stations --csv /path/to/fuel-prices.csv
```

### 5. Start the async dev server

```bash
uvicorn fuel_finder.asgi:application --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000) for the interactive map UI.

---

## Docker Compose (recommended for demo)

```bash
# 1. Build and start
docker compose up --build

# 2. In a separate terminal — import stations into the container
docker compose exec web python manage.py import_stations

# 3. Open http://localhost:8000
```

---

## API reference

### `POST /api/route/`

Compute the cheapest fuel stop plan.

**Request body (JSON):**

```json
{
  "origin": "Chicago, IL",
  "destination": "Los Angeles, CA",
  "mpg": 10,
  "tank_range_miles": 500
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `origin` | string | required | Starting location (free-text US address) |
| `destination` | string | required | Ending location (free-text US address) |
| `mpg` | float | `10.0` | Vehicle fuel efficiency (miles per gallon) |
| `tank_range_miles` | float | `500.0` | Maximum range on a full tank (miles) |

**Response (200 OK):**

```json
{
  "origin": "Chicago, IL",
  "destination": "Los Angeles, CA",
  "total_distance_miles": 2015.3,
  "total_fuel_cost": 612.40,
  "total_gallons": 201.5,
  "fuel_stops": [
    {
      "station_id": 1234,
      "name": "PILOT TRAVEL CENTER #42",
      "address": "I-80, EXIT 318",
      "city": "Iowa City",
      "state": "IA",
      "retail_price": 3.049,
      "latitude": 41.66,
      "longitude": -91.53,
      "gallons_to_fill": 48.3,
      "cost_at_stop": 147.27,
      "miles_from_previous": 283.0
    }
  ],
  "route_geometry": [[-87.62, 41.88], [-88.01, 41.73], "..."]
}
```

`route_geometry` is a GeoJSON LineString coordinates array (`[lon, lat]`).

### `GET /api/health/`

```json
{ "status": "ok", "geocoded_stations": 7841 }
```

---

## Project structure

```
assignment3/
├── fuel_finder/            # Django project config
│   ├── settings.py         # Env-driven settings
│   ├── urls.py             # Root URL conf
│   └── asgi.py             # ASGI entry point (uvicorn)
├── stations/               # Fuel station data app
│   ├── models.py           # FuelStation model
│   ├── admin.py            # Admin panel config
│   └── management/
│       └── commands/
│           └── import_stations.py   # CSV import + async geocoding
├── router/                 # Route optimization app
│   ├── views.py            # Async API views
│   ├── serializers.py      # DRF serializers
│   ├── urls.py             # App URL conf
│   └── services/
│       ├── osrm.py         # OSRM API client (1 call per request)
│       ├── geocoding.py    # Nominatim geocoding (origin/dest)
│       └── optimizer.py    # Greedy fuel stop optimizer (NumPy)
├── templates/
│   └── index.html          # Map UI (Leaflet + Vanilla JS)
├── static/
│   ├── css/main.css
│   └── js/app.js
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

---

## Algorithm — how the optimizer works

1. **Single OSRM call** — get full route geometry + total distance.
2. **Sample waypoints** every 20% of tank range along the polyline.
3. **Greedy look-ahead**:
   - Start at origin with a full tank.
   - At each position, find all stations reachable within remaining range **and** closer to the destination.
   - Pick the **cheapest** one.
   - Fill up to a full tank, move there, repeat.
   - Stop when destination is within remaining range.
4. **NumPy bounding-box pre-filter** before Haversine — makes candidate search O(1) on average.

---

## Design decisions

- **No page reloads** — frontend uses `fetch()` against the REST API; Django template is just a thin shell.
- **Async everywhere** — Django async views + `httpx.AsyncClient` + `asyncio.gather` for concurrent geocoding.
- **One OSRM call per request** — the spec asked for minimal external calls; waypoints are sampled in-process from the returned geometry.
- **Pre-geocoded stations** — geocoding happens once at import time, not per request.
- **SQLite default** — zero config for local dev; switch to PostgreSQL via `DATABASE_URL` env var.
